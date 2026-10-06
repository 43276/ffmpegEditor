"""元数据页面组装；表格、输入弹窗和草稿任务分别由专用组件负责。"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QDialog, QFileDialog, QGridLayout, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import Action, CaptionLabel, CheckableMenu, ComboBox, DropDownPushButton, InfoBar, LineEdit, PushButton, SegmentedWidget, StrongBodyLabel

from app.audio.formats import METADATA_FIELDS, WAV_AUTO_CONVERT_TARGETS, field_by_key
from app.audio.models import TrackEdit
from app.task_models import LOG_INFO, LOG_WARN, TaskProgress, TaskResult, TaskStatistics
from ui.services.desktop_actions import DesktopActions
from ui.tasks.controller import TaskState
from ui.widgets.log_panel import LogPanel
from ui.widgets.page_layout import build_page_content, make_card
from ui.widgets.smooth_scroll import SmoothScrollArea
from ui.widgets.switch_button import MakeSwitchButton
from ui.widgets.task_panel import TaskPanel
from .controller import MetadataController
from .dialogs import CoverDialog, FormatDialog, TextDialog
from .table import MetadataTable


class MetadataPage(QWidget):
    def __init__(self, parent=None, *, settings=None, desktop_actions=None):
        super().__init__(parent)
        self.setObjectName("metadataPage")
        self._settings = settings
        self._desktop_actions = desktop_actions or DesktopActions()
        self._controller = MetadataController(self)
        self._task_controller = self._controller.tasks
        self._checked_fields = self._controller.checked_fields
        self._cover_temp_path = self._controller.resources.path
        self._cover_zoom = "small"
        self._write_mode = "inplace"
        self._closing = False
        self._close_requested = False
        self._error_logs: list[str] = []
        self._last_output_dirs: list[str] = []
        self._BuildUi()
        self._ConnectSignals()
        self._UpdateControls()

    @property
    def _rows(self) -> list[TrackEdit]:
        return self._controller.rows

    @_rows.setter
    def _rows(self, rows) -> None:
        self._controller.replace_rows(rows)

    @property
    def _ffmpeg_path(self) -> str | None:
        return self._controller.ffmpeg_path

    @_ffmpeg_path.setter
    def _ffmpeg_path(self, path) -> None:
        self._controller.ffmpeg_path = path

    @property
    def _ffprobe_path(self) -> str | None:
        return self._controller.ffprobe_path

    @_ffprobe_path.setter
    def _ffprobe_path(self, path) -> None:
        self._controller.ffprobe_path = path

    def _BuildUi(self) -> None:
        self.setAcceptDrops(True)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.scroll_area = SmoothScrollArea(self)
        root.addWidget(self.scroll_area)
        _content, self.content_layout = build_page_content(
            self.scroll_area, "元数据编辑",
            "基于 ffprobe / ffmpeg · 导入音频后在表格中编辑元数据、封面与格式，确认后写入",
            object_name="metadataContent")
        self._BuildInputCard()
        self._BuildParamsCard()
        self._BuildTableCard()
        self._BuildLogCard()
        self.content_layout.addStretch(1)

    def _MakeCard(self, title):
        return make_card(self.scroll_area, title)

    def AddBottomWidget(self, widget: QWidget) -> None:
        self.content_layout.insertWidget(self.content_layout.count() - 1, widget)

    @staticmethod
    def _MakeFieldLabel(text: str, parent: QWidget) -> StrongBodyLabel:
        label = StrongBodyLabel(text, parent)
        label.setFixedWidth(96)
        return label

    def _BuildInputCard(self) -> None:
        card, layout = self._MakeCard("导入音频")
        self.content_layout.addWidget(card)

        button_row = QHBoxLayout()
        self.import_files_button = PushButton("导入文件…", card)
        self.import_folder_button = PushButton("导入文件夹…", card)
        button_row.addWidget(self.import_files_button)
        button_row.addWidget(self.import_folder_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)



    def _BuildParamsCard(self) -> None:
        card, layout = self._MakeCard("编辑参数")
        self.content_layout.addWidget(card)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)

        # 字段多选下拉
        grid.addWidget(self._MakeFieldLabel("元数据字段", card), 0, 0)
        field_row = QHBoxLayout()
        self.field_dropdown = DropDownPushButton("选择字段", card)
        field_row.addWidget(self.field_dropdown)
        field_row.addSpacing(12)
        field_row.addWidget(
            CaptionLabel("未勾选的字段不会显示、也不会写入", card)
        )
        field_row.addStretch(1)
        grid.addLayout(field_row, 0, 1)

        # 封面缩放等级（仅勾选封面时显示）
        self.cover_zoom_label = self._MakeFieldLabel("封面缩放", card)
        grid.addWidget(self.cover_zoom_label, 1, 0)
        zoom_row = QHBoxLayout()
        self.cover_zoom_seg = SegmentedWidget(card)
        self.cover_zoom_seg.addItem("small", "小")
        self.cover_zoom_seg.addItem("large", "大")
        self.cover_zoom_seg.setCurrentItem("small")
        zoom_row.addWidget(self.cover_zoom_seg)
        zoom_row.addSpacing(12)
        zoom_row.addStretch(1)
        self.cover_zoom_row = zoom_row
        grid.addLayout(zoom_row, 1, 1)

        # wav 加封面自动转换
        self.wav_cover_label = self._MakeFieldLabel("WAV 封面", card)
        grid.addWidget(self.wav_cover_label, 2, 0)
        wav_row = QHBoxLayout()
        self.wav_cover_switch = MakeSwitchButton("为 wav 添加封面时自动转换为", card)
        self.wav_target_combo = ComboBox(card)
        for item in WAV_AUTO_CONVERT_TARGETS:
            self.wav_target_combo.addItem(item.display_name, userData=item.key)
        self.wav_target_combo.setCurrentIndex(0)
        wav_row.addWidget(self.wav_cover_switch)
        wav_row.addWidget(self.wav_target_combo)
        wav_row.addStretch(1)
        self.wav_row = wav_row
        grid.addLayout(wav_row, 2, 1)

        # 修改方式
        grid.addWidget(self._MakeFieldLabel("修改方式", card), 3, 0)
        mode_row = QHBoxLayout()
        self.write_mode_seg = SegmentedWidget(card)
        self.write_mode_seg.addItem("inplace", "覆盖")
        self.write_mode_seg.addItem("output", "另存为")
        self.write_mode_seg.setCurrentItem("inplace")
        mode_row.addWidget(self.write_mode_seg)
        mode_row.addSpacing(12)
        mode_row.addWidget(
            CaptionLabel(card)
        )
        mode_row.addStretch(1)
        grid.addLayout(mode_row, 3, 1)

        # 输出目录（输出模式）
        self.output_root_label = self._MakeFieldLabel("输出目录", card)
        grid.addWidget(self.output_root_label, 4, 0)
        output_row = QHBoxLayout()
        self.output_root_line = LineEdit(card)
        self.output_root_line.setPlaceholderText("另存为时必填")
        self.output_root_line.setClearButtonEnabled(True)
        self.output_root_browse = PushButton("浏览…", card)
        output_row.addWidget(self.output_root_line, 1)
        output_row.addWidget(self.output_root_browse)
        self.output_root_row = output_row
        grid.addLayout(output_row, 4, 1)

        # 覆盖与备份
        grid.addWidget(self._MakeFieldLabel("同名输出", card), 5, 0)
        same_row = QHBoxLayout()
        self.overwrite_switch = MakeSwitchButton("覆盖已存在的输出文件", card)
        self.overwrite_switch.setChecked(True)
        same_row.addWidget(self.overwrite_switch)
        same_row.addSpacing(24)
        self.backup_switch = MakeSwitchButton("覆盖前备份源文件 (.bak)", card)
        same_row.addWidget(self.backup_switch)
        same_row.addStretch(1)
        grid.addLayout(same_row, 5, 1)

        layout.addLayout(grid)

        self.ffmpeg_status_label = CaptionLabel("正在检测 ffmpeg…", card)
        layout.addWidget(self.ffmpeg_status_label)

    def _BuildTableCard(self) -> None:
        self.task_panel = TaskPanel(self.scroll_area, start_text="确认修改", supports_finish=False,
                                    show_exports=False)
        self.task_panel.setTitle("预览与编辑")
        self.content_layout.addWidget(self.task_panel)
        self.status_label = self.task_panel.status_label
        self.status_label.setText("尚未导入文件")
        self.task_panel.body_layout.removeWidget(self.status_label)
        self.task_panel.button_row.takeAt(self.task_panel.button_row.count() - 1)
        self.task_panel.button_row.insertWidget(0, self.status_label)
        self.task_panel.button_row.insertStretch(1, 1)
        self.reset_button = PushButton("重置全部修改", self.task_panel)
        self.task_panel.button_row.insertWidget(2, self.reset_button)
        self.confirm_button = self.task_panel.start_button
        self.cancel_button = self.task_panel.cancel_button
        self.open_output_button = self.task_panel.open_folder_button
        self.progress_bar = self.task_panel.progress_bar
        self.table = MetadataTable(self.task_panel)
        self.task_panel.body_layout.insertWidget(1, self.table)
        self.task_panel.body_layout.addWidget(CaptionLabel(
            "点击列标题统一修改该列；点击单元格只修改该行；.ext1->.ext2 码率 表示将转换。",
            self.task_panel))

    def _BuildLogCard(self) -> None:
        self.log_panel = LogPanel(self.scroll_area, hint="")
        self.content_layout.addWidget(self.log_panel)
        self.log_browser = self.log_panel.browser
        self.log_statistics_label = self.log_panel.statistics_label
        self.clear_log_button = self.log_panel.clear_button
        self.export_error_button = self.task_panel.export_error_button
        self.task_panel.button_row.removeWidget(self.export_error_button)
        top_row = self.log_panel.body_layout.itemAt(0).layout()
        top_row.insertWidget(top_row.count() - 1, self.export_error_button)
        self.export_error_button.show()

    def _ConnectSignals(self) -> None:
        self.import_files_button.clicked.connect(self._OnImportFiles)
        self.import_folder_button.clicked.connect(self._OnImportFolder)
        self.field_dropdown.clicked.connect(self._OnFieldMenuClicked)
        self.cover_zoom_seg.currentItemChanged.connect(self._OnCoverZoomChanged)
        self.wav_cover_switch.checkedChanged.connect(lambda _checked: self._UpdateControls())
        self.write_mode_seg.currentItemChanged.connect(self._OnWriteModeChanged)
        self.output_root_browse.clicked.connect(self._OnBrowseOutputRoot)
        self.reset_button.clicked.connect(self._OnResetClicked)
        self.confirm_button.clicked.connect(self._OnConfirmClicked)
        self.cancel_button.clicked.connect(self._OnCancelClicked)
        self.open_output_button.clicked.connect(self._OnOpenOutputDir)
        self.export_error_button.clicked.connect(self._ExportErrorLog)
        self.table.columnRequested.connect(self._OnColumnRequested)
        self.table.formatRequested.connect(self._OpenFormatDialog)
        self.table.coverRequested.connect(self._OpenCoverDialog)
        self.table.textEdited.connect(self._controller.edit_text)
        self._controller.rowsReset.connect(self._RebuildTable)
        self._controller.rowAdded.connect(self.table.add_row)
        self._controller.rowChanged.connect(self.table.update_row)
        self._controller.modifiedChanged.connect(lambda _count: self._UpdateModifiedStatus())
        self._controller.actionsChanged.connect(self._UpdateControls)
        self._controller.logMessage.connect(lambda event: self._AppendLog(event.level, event.message))
        self._controller.progressChanged.connect(self._OnTaskProgress)
        self._controller.statisticsChanged.connect(self._OnTaskStatistics)
        self._controller.started.connect(self._OnTaskStarted)
        self._controller.completed.connect(self._OnTaskFinished)
        self._controller.notice.connect(self._OnNotice)
        self._controller.idle.connect(self._TryCleanupCovers)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls() and not self._closing and not self._task_controller.active:
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self._StartImport(paths)
            event.acceptProposedAction()

    def _OnImportFiles(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择音频文件", "",
            "音频文件 (*.mp3 *.m4a *.mp4 *.aac *.flac *.ogg *.opus *.wav *.wma *.alac);;所有文件 (*.*)")
        if paths:
            self._StartImport([Path(path) for path in paths])

    def _OnImportFolder(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择音频文件夹", "")
        if directory:
            self._StartImport([Path(directory)])

    def _StartImport(self, paths) -> None:
        self._controller.start_import(paths)

    def _OnTaskStarted(self, kind: str, total: int) -> None:
        self._error_logs = []
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(0)
        self.status_label.setText("正在收集音频文件…" if kind == "read" else "正在写入…")
        self._UpdateControls()

    def _RebuildTable(self) -> None:
        self.table.set_entries(self._controller.entries)

    def _OnFieldMenuClicked(self) -> None:
        menu = CheckableMenu(parent=self)
        for item in METADATA_FIELDS:
            action = Action("封面" if item.kind == "cover" else item.display_name, menu)
            action.setCheckable(True)
            action.setChecked(item.key in self._checked_fields)
            action.toggled.connect(lambda checked, key=item.key: self._OnFieldToggled(key, checked))
            menu.addAction(action)
        menu.exec(self.field_dropdown.mapToGlobal(self.field_dropdown.rect().bottomLeft()))

    def _OnFieldToggled(self, key: str, checked: bool) -> None:
        self._controller.set_field_visible(key, checked)
        self.table.set_fields(self._checked_fields)
        self._UpdateControls()

    def _OnCoverZoomChanged(self, key: str) -> None:
        self._cover_zoom = key
        self.table.set_cover_zoom(key)

    def _OnWriteModeChanged(self, key: str) -> None:
        self._write_mode = key
        self._UpdateControls()

    def _OnBrowseOutputRoot(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "选择输出目录", self.output_root_line.text().strip())
        if directory:
            self.output_root_line.setText(directory)

    def _OnColumnRequested(self, kind: str, key: str) -> None:
        if kind == "format":
            self._OpenFormatDialog(None)
        elif kind == "cover":
            self._OpenCoverDialog(None)
        elif kind == "text":
            self._OpenTextDialog(field_by_key(key), None)

    def _ScopeText(self, row_id: str | None) -> str:
        row = self._controller.row(row_id) if row_id is not None else None
        return f"仅修改：{row.file_name}" if row is not None else f"将统一应用到全部 {len(self._rows)} 个文件"

    def _CanEdit(self) -> bool:
        return bool(self._rows) and not self._closing and not self._task_controller.active

    def _OpenTextDialog(self, field, row_id: str | None) -> None:
        if not self._CanEdit() or field is None:
            return
        dialog = TextDialog(self, field, self._ScopeText(row_id))
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted and dialog.value is not None:
                self._controller.apply_text(field.key, dialog.value, row_id)
        finally:
            dialog.deleteLater()

    def _OpenFormatDialog(self, row_id: str | None) -> None:
        if not self._CanEdit():
            return
        row = self._controller.row(row_id) if row_id is not None else None
        dialog = FormatDialog(self, self._ScopeText(row_id),
                              row.target_format_key if row else None, row.bitrate if row else None)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted and dialog.format_key is not None:
                self._controller.apply_format(dialog.format_key, dialog.bitrate, row_id)
        finally:
            dialog.deleteLater()

    def _OpenCoverDialog(self, row_id: str | None) -> None:
        if not self._CanEdit():
            return
        ffmpeg = self._EnsureFfmpeg()
        if not ffmpeg:
            return
        items = [(row.path, row.cover_codec) for row in self._rows if not row.error] if row_id is None else None
        dialog = CoverDialog(self, self._ScopeText(row_id), ffmpeg, items, self._controller.resources)
        try:
            if (dialog.exec() == QDialog.DialogCode.Accepted and not self._closing
                    and dialog.result_action is not None):
                self._ApplyCoverResult(dialog.result_action, dialog.cover_source, row_id)
        finally:
            dialog.deleteLater()

    def _ApplyCoverResult(self, action: str, source: Path | None, row_id: str | None) -> None:
        self._controller.apply_cover(
            action, source, row_id, auto_convert_wav=self.wav_cover_switch.isChecked(),
            wav_target_key=self.wav_target_combo.itemData(self.wav_target_combo.currentIndex()))

    def _OnConfirmClicked(self) -> None:
        self._controller.confirm_write(
            overwrite=self.overwrite_switch.isChecked(), in_place=self._write_mode == "inplace",
            output_root=self.output_root_line.text().strip() or None,
            backup=self.backup_switch.isChecked())

    def _OnCancelClicked(self) -> None:
        if self._task_controller.request_cancel():
            self.status_label.setText("正在取消…")
            self._UpdateControls()

    def _OnResetClicked(self) -> None:
        self._controller.reset()

    def _OnTaskProgress(self, progress: TaskProgress) -> None:
        previous = self.status_label.text()
        self.task_panel.set_progress(progress, show_file=self._task_controller.kind == "read")
        if self._task_controller.state != TaskState.RUNNING:
            self.status_label.setText(previous)
        else:
            action = "导入" if self._task_controller.kind == "read" else "写入"
            suffix = f"：{progress.current_file}" if action == "导入" else ""
            self.status_label.setText(f"正在{action} {progress.done}/{progress.total}{suffix}")

    def _OnTaskStatistics(self, stats: TaskStatistics) -> None:
        self.task_panel.set_statistics(stats)
        self.log_panel.set_statistics(stats)
        self.progress_bar.setRange(0, max(1, stats.total))
        self.progress_bar.setValue(stats.done)

    def _OnTaskFinished(self, kind: str, summary: TaskResult) -> None:
        if self._closing:
            return
        self.progress_bar.setRange(0, max(1, summary.total))
        self.progress_bar.setValue(summary.ok + summary.failed + summary.skipped)
        self.task_panel.set_result(summary, action_text="导入" if kind == "read" else "写入")
        self.log_panel.set_statistics(summary)
        self._error_logs = list(summary.error_logs)
        if kind == "read":
            if not summary.cancelled and not summary.failed and not self._rows:
                self._ShowInfoBar("没有找到音频", "所选输入中没有找到支持的音频文件", warning=True)
            elif summary.cancelled:
                self.status_label.setText(f"导入已取消 · 共 {len(self._rows)} 个文件")
                self._ShowInfoBar("导入已取消", f"已读取 {summary.ok} 个文件", warning=True)
            elif summary.failed:
                self._ShowInfoBar("导入完成（部分失败）", f"成功 {summary.ok}，失败 {summary.failed}", warning=True)
            else:
                self.status_label.setText(f"共 {len(self._rows)} 个文件")
                self._ShowInfoBar("导入完成", f"共读取 {summary.ok} 个文件")
            parents = {str(row.path.resolve().parent) for row in self._rows}
            if len(parents) == 1:
                self.output_root_line.setText(next(iter(parents)))
        else:
            self._last_output_dirs = list(summary.output_dirs)
            for directory in self._last_output_dirs[:5]:
                self._AppendLog(LOG_INFO, f"输出目录：{directory}")
            title = "写入已取消" if summary.cancelled else "写入完成（有失败项）" if summary.failed else "写入完成"
            self._ShowInfoBar(title, f"成功 {summary.ok}，失败 {summary.failed}，跳过 {summary.skipped}",
                              warning=summary.cancelled or bool(summary.failed))
        self._UpdateControls()

    def SetEnvironment(self, snapshot) -> None:
        self._controller.set_environment(snapshot)
        if snapshot.status == "ready":
            probe = f"ffprobe: {snapshot.ffprobe_path}" if snapshot.ffprobe_path else "✗ 未找到 ffprobe"
            self.ffmpeg_status_label.setText(f"✓ ffmpeg: {snapshot.ffmpeg_path}    {probe}")
        else:
            self.ffmpeg_status_label.setText(snapshot.message or snapshot.error or "FFmpeg 未就绪")
        self._UpdateControls()

    def SetFfmpegPath(self, path: str | None, ffprobe_path: str | None = None) -> None:
        self._ffmpeg_path, self._ffprobe_path = path, ffprobe_path
        self.ffmpeg_status_label.setText(f"ffmpeg: {path}" if path else "FFmpeg 未就绪")
        self._UpdateControls()

    def _EnsureFfmpeg(self) -> str | None:
        if self._ffmpeg_path:
            return self._ffmpeg_path
        self._ShowInfoBar("无法开始", "FFmpeg 尚未就绪，请在设置页检查路径", error=True)
        return None

    def _UpdateControls(self) -> None:
        running = self._task_controller.active or self._closing
        for widget in (self.import_files_button, self.import_folder_button, self.field_dropdown, self.table,
                       self.cover_zoom_seg, self.wav_cover_switch, self.write_mode_seg,
                       self.output_root_line, self.output_root_browse, self.overwrite_switch):
            widget.setEnabled(not running)
        has_cover = "cover" in self._checked_fields
        self._ShowRow(self.cover_zoom_row, has_cover)
        self.cover_zoom_label.setVisible(has_cover)
        self._ShowRow(self.wav_row, has_cover)
        self.wav_cover_label.setVisible(has_cover)
        self.wav_target_combo.setEnabled(not running and self.wav_cover_switch.isChecked())
        in_place = self._write_mode == "inplace"
        self._ShowRow(self.output_root_row, not in_place)
        self.output_root_label.setVisible(not in_place)
        self.backup_switch.setVisible(in_place)
        self.backup_switch.setEnabled(not running and in_place)
        modified = self._controller.modified_count if not running else 0
        self.reset_button.setEnabled(not running and any(row.modified for row in self._rows))
        self.task_panel.update_state(self._task_controller, ready=modified > 0 and bool(self._ffmpeg_path), closing=self._closing,
                                     has_output=bool(self._last_output_dirs), has_errors=bool(self._error_logs))

    @staticmethod
    def _ShowRow(row: QHBoxLayout, visible: bool) -> None:
        for index in range(row.count()):
            widget = row.itemAt(index).widget()
            if widget is not None:
                widget.setVisible(visible)

    def _UpdateModifiedStatus(self) -> None:
        if self._task_controller.active or self._closing:
            return
        count = self._controller.modified_count
        if not self._rows:
            self.status_label.setText("尚未导入文件")
        elif count:
            self.status_label.setText(f"共 {len(self._rows)} 个文件 · 已修改 {count} 个（未确认）")
        else:
            self.status_label.setText(f"共 {len(self._rows)} 个文件")
        self._UpdateControls()

    def _AppendLog(self, level, text: str) -> None:
        self.log_panel.append(level, text)

    def _ExportErrorLog(self) -> None:
        if not self._task_controller.active and not self._closing:
            LogPanel.export_entries(self, "导出错误日志", "error_log.txt", self._error_logs, self._ShowInfoBar)

    def _OnOpenOutputDir(self) -> None:
        if len(self._last_output_dirs) > 5:
            self._AppendLog(LOG_WARN, "输出目录较多，已打开前 5 个")
        for directory in self._last_output_dirs[:5]:
            try:
                if Path(directory).is_dir():
                    self._desktop_actions.open_output_directory(directory)
            except OSError as exc:
                self._ShowInfoBar("打开失败", str(exc), error=True)

    def _OnNotice(self, title: str, content: str, kind: str) -> None:
        self._ShowInfoBar(title, content, error=kind == "error", warning=kind == "warning")

    def _ShowInfoBar(self, title: str, content: str, error: bool = False, warning: bool = False) -> None:
        if not self._closing:
            action = InfoBar.error if error else InfoBar.warning if warning else InfoBar.success
            action(title, content, parent=self)

    def Shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        for dialog in self.findChildren(CoverDialog):
            dialog.idle.connect(self._TryCleanupCovers)
            dialog.Shutdown()
        self._controller.shutdown()
        self._UpdateControls()
        self._TryCleanupCovers()

    def _TryCleanupCovers(self) -> None:
        if (self._closing and not self._task_controller.active
                and not any(dialog.active for dialog in self.findChildren(CoverDialog))):
            self._controller.resources.request_cleanup()
            if self._close_requested:
                QTimer.singleShot(0, self.close)

    def closeEvent(self, event) -> None:
        self._close_requested = True
        self.Shutdown()
        if self._task_controller.active or any(dialog.active for dialog in self.findChildren(CoverDialog)):
            event.ignore()
        else:
            event.accept()
