"""音频元数据编辑页面
"""
from __future__ import annotations

import html
import os
import tempfile
import time
import weakref
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QBuffer, QIODevice, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QImage, QKeySequence, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    Action,
    CaptionLabel,
    CheckableMenu,
    ComboBox,
    DropDownPushButton,
    HeaderCardWidget,
    InfoBar,
    LineEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    ScrollArea,
    SegmentedWidget,
    StrongBodyLabel,
    TableWidget,
    TextBrowser,
    TitleLabel,
)
from qfluentwidgets.common.style_sheet import isDarkTheme

from app.AddCover import AUDIO_EXTENSIONS, IMAGE_EXTENSIONS
from app.Converter import ConverterError, LocateFfmpeg
from app.MetadataEdit import (
    DEFAULT_BITRATE,
    FORMAT_OPTIONS,
    METADATA_FIELDS,
    TEXT_FIELDS,
    WAV_AUTO_CONVERT_TARGETS,
    CollectAudioFiles,
    FieldByKey,
    FormatByKey,
    FormatDisplay,
    LocateFfprobe,
    MetadataError,
    TrackEdit,
)
from ui.Controls import MakeSwitchButton
from ui.MetadataWorker import CoverExportWorker, MetadataReadWorker, MetadataWriteWorker
from ui.Worker import LOG_ERROR, LOG_INFO, LOG_OK, LOG_WARN

_LOG_COLORS = {
    LOG_OK: ("#0f7b0f", "#7adfa0"),
    LOG_WARN: ("#9a6700", "#f5c26b"),
    LOG_ERROR: ("#c42b1c", "#ff9aa2"),
}
_LEVEL_MARKS = {LOG_OK: "✓ ", LOG_WARN: "⚠ ", LOG_ERROR: "✗ "}

_COVER_THUMB_SMALL = 48
_COVER_THUMB_LARGE = 96
_ROW_HEIGHT_SMALL = 58
_ROW_HEIGHT_LARGE = 106
_COVER_COL_SMALL = 104
_COVER_COL_LARGE = 152


class _ClickableLabel(QLabel):
    """可点击的 QLabel：封面单元格与格式单元格使用。"""

    clicked = pyqtSignal()

    def mousePressEvent(self, event) -> None:  # noqa: N802 —— Qt 事件
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class _MarqueeLabel(QWidget):
    """文件名跑马灯：文本超出列宽时共享定时器向左滚动。"""

    # 弱引用集合：表格重建会丢弃大量旧标签，避免实例被永久持有
    _instances: "weakref.WeakSet[_MarqueeLabel]" = weakref.WeakSet()
    _timer: QTimer | None = None

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._text = text
        self._offset = 0.0
        self.setMinimumHeight(24)
        self._EnsureTimer()
        _MarqueeLabel._instances.add(self)

    def setText(self, text: str) -> None:  # noqa: N802 —— 覆盖 QWidget 语义
        self._text = text
        self._offset = 0.0
        self.update()

    def _TextWidth(self) -> int:
        return self.fontMetrics().horizontalAdvance(self._text)

    @classmethod
    def _EnsureTimer(cls) -> None:
        if cls._timer is None:
            cls._timer = QTimer()
            cls._timer.setInterval(60)
            cls._timer.timeout.connect(cls._Tick)
            cls._timer.start()

    @classmethod
    def _Tick(cls) -> None:
        dead: list[_MarqueeLabel] = []
        for widget in cls._instances:
            try:
                if widget.isHidden():
                    continue
                text_width = widget._TextWidth()
                if text_width > widget.width():
                    widget._offset += 1
                    if widget._offset > text_width + 40:
                        widget._offset = 0
                    widget.update()
                elif widget._offset:
                    widget._offset = 0
                    widget.update()
            except RuntimeError:
                dead.append(widget)
        for widget in dead:
            cls._instances.remove(widget)

    def paintEvent(self, event) -> None:  # noqa: N802 —— Qt 事件
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        rect = self.rect()
        text_width = self._TextWidth()
        if text_width <= rect.width():
            painter.drawText(
                rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                self._text,
            )
            return
        painter.save()
        painter.setClipRect(rect)
        gap = 40
        x = -int(self._offset)
        for start in (x, x + text_width + gap):
            painter.drawText(
                QRect(start, 0, text_width + gap, rect.height()),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                self._text,
            )
        painter.restore()


class _CellEditor(QWidget):
    """文本单元格容器：把输入框居中并留边，使其比表格格子小。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.line_edit = LineEdit(self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 5, 12, 5)
        layout.addWidget(self.line_edit)


class _TextDialog(QDialog):
    """文本类元数据的统一操作小窗（点栏目=全部改为，点条目=只改该行）。"""

    def __init__(self, parent, field, scope_text: str):
        super().__init__(parent)
        self.setWindowTitle(f"统一修改 · {field.display_name}")
        self.setModal(True)
        self.setMinimumWidth(420)
        self.value: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        layout.addWidget(CaptionLabel(scope_text, self))
        self.edit = LineEdit(self)
        self.edit.setPlaceholderText("留空则不修改")
        self.edit.setClearButtonEnabled(True)
        layout.addWidget(self.edit)
        layout.addWidget(
            CaptionLabel(
                "留空表示不修改；如需删除该字段，请在表格中把对应文本框清空。",
                self,
            )
        )

        if field.kind == "artist":
            layout.addWidget(
                CaptionLabel(
                    "作者支持分号分隔，例如：name1;name2;name3，将直接作为参数写入。",
                    self,
                )
            )

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel_button = PushButton("取消", self)
        ok_button = PrimaryPushButton("确定", self)
        buttons.addWidget(cancel_button)
        buttons.addWidget(ok_button)
        layout.addLayout(buttons)

        ok_button.clicked.connect(self._OnOk)
        cancel_button.clicked.connect(self.reject)

    def _OnOk(self) -> None:
        text = self.edit.text().strip()
        if text:
            self.value = text
        self.accept()


class _FormatDialog(QDialog):
    """格式转换小窗：目标格式 + 压缩选项（有损码率 / 无损无选项）。"""

    def __init__(self, parent, scope_text: str, current_key: str | None, current_bitrate: str | None):
        super().__init__(parent)
        self.setWindowTitle("格式转换")
        self.setModal(True)
        self.setMinimumWidth(440)
        self.format_key: str | None = None
        self.bitrate: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        layout.addWidget(CaptionLabel(scope_text, self))

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.addWidget(StrongBodyLabel("目标格式", self), 0, 0)
        self.format_combo = ComboBox(self)
        for item in FORMAT_OPTIONS:
            self.format_combo.addItem(item.display_name, userData=item.key)
        grid.addWidget(self.format_combo, 0, 1)

        grid.addWidget(StrongBodyLabel("压缩选项", self), 1, 0)
        self.bitrate_combo = ComboBox(self)
        grid.addWidget(self.bitrate_combo, 1, 1)
        layout.addLayout(grid)

        hint = CaptionLabel("仅元数据修改时保持原格式、不重编码；选择新格式才重编码。", self)
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        clear_button = PushButton("清除转换", self)
        clear_button.setToolTip("恢复为保持原格式")
        buttons.addWidget(clear_button)
        buttons.addStretch(1)
        cancel_button = PushButton("取消", self)
        ok_button = PrimaryPushButton("确定", self)
        buttons.addWidget(cancel_button)
        buttons.addWidget(ok_button)
        layout.addLayout(buttons)

        index = self.format_combo.findData(current_key or "keep")
        self.format_combo.setCurrentIndex(index if index >= 0 else 0)
        self._current_bitrate = current_bitrate
        self._UpdateBitrate()

        self.format_combo.currentIndexChanged.connect(self._UpdateBitrate)
        clear_button.clicked.connect(self._OnClear)
        ok_button.clicked.connect(self._OnOk)
        cancel_button.clicked.connect(self.reject)

    def _UpdateBitrate(self) -> None:
        key = self.format_combo.itemData(self.format_combo.currentIndex())
        fmt = FormatByKey(key)
        self.bitrate_combo.clear()
        if fmt is None or not fmt.lossy:
            self.bitrate_combo.addItem("无损 / 无压缩选项", userData=None)
            self.bitrate_combo.setEnabled(False)
            return
        for rate in fmt.bitrates:
            self.bitrate_combo.addItem(rate, userData=rate)
        self.bitrate_combo.setEnabled(True)
        wanted = self._current_bitrate if self._current_bitrate in fmt.bitrates else DEFAULT_BITRATE
        index = self.bitrate_combo.findData(wanted)
        self.bitrate_combo.setCurrentIndex(index if index >= 0 else 0)

    def _OnClear(self) -> None:
        self.format_key = "keep"
        self.bitrate = None
        self.accept()

    def _OnOk(self) -> None:
        key = self.format_combo.itemData(self.format_combo.currentIndex())
        self.format_key = key
        if key and key != "keep":
            fmt = FormatByKey(key)
            if fmt is not None and fmt.lossy:
                self.bitrate = self.bitrate_combo.itemData(self.bitrate_combo.currentIndex())
        self.accept()


class _CoverDialog(QDialog):
    """封面小窗：选择文件 / 粘贴图片 / 提取全部封面 / 移除封面。"""

    def __init__(self, parent, scope_text: str, ffmpeg_path: str, export_items, temp_dir: Path):
        super().__init__(parent)
        self.setWindowTitle("封面")
        self.setModal(True)
        self.setMinimumWidth(480)
        self.ffmpeg_path = ffmpeg_path
        self.export_items = export_items
        self.temp_dir = temp_dir
        self.result_action: str | None = None   # "set" | "remove" | None
        self.cover_source: Path | None = None
        self._remove_requested = False
        self._export_worker: CoverExportWorker | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        layout.addWidget(CaptionLabel(scope_text, self))

        button_row = QHBoxLayout()
        choose_button = PushButton("选择图片文件…", self)
        paste_button = PushButton("粘贴图片", self)
        export_button = PushButton("提取全部封面…", self)
        remove_button = PushButton("移除封面", self)
        button_row.addWidget(choose_button)
        button_row.addWidget(paste_button)
        button_row.addWidget(export_button)
        button_row.addWidget(remove_button)
        layout.addLayout(button_row)

        preview_row = QHBoxLayout()
        self.preview = QLabel(self)
        self.preview.setFixedSize(140, 140)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setText("未选择图片")
        preview_row.addWidget(self.preview)
        info_layout = QVBoxLayout()
        info_layout.addWidget(
            CaptionLabel("支持选择图片文件或直接粘贴图片（Ctrl+V）。", self)
        )
        info_layout.addWidget(
            CaptionLabel("提取全部封面：从磁盘上的原始音频提取，未确认的内存修改不算。", self)
        )
        info_layout.addStretch(1)
        preview_row.addLayout(info_layout, 1)
        layout.addLayout(preview_row)

        self.status_label = CaptionLabel("", self)
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel_button = PushButton("取消", self)
        ok_button = PrimaryPushButton("确定", self)
        buttons.addWidget(cancel_button)
        buttons.addWidget(ok_button)
        layout.addLayout(buttons)

        self._buttons = [choose_button, paste_button, export_button, remove_button, ok_button]

        choose_button.clicked.connect(self._OnChoose)
        paste_button.clicked.connect(self._OnPaste)
        export_button.clicked.connect(self._OnExportAll)
        remove_button.clicked.connect(self._OnRemove)
        ok_button.clicked.connect(self._OnOk)
        cancel_button.clicked.connect(self.reject)

    # ---- 封面选择 -----------------------------------------------------
    def _OnChoose(self) -> None:
        file_path, _filter = QFileDialog.getOpenFileName(
            self,
            "选择封面图片",
            "",
            "图片文件 (*.jpg *.jpeg *.png *.webp *.bmp);;所有文件 (*.*)",
        )
        if file_path:
            self._SetCoverSource(Path(file_path))

    def _OnPaste(self) -> None:
        mime = QApplication.clipboard().mimeData()
        if mime.hasImage():
            image = QApplication.clipboard().image()
            if not image.isNull():
                self._SetCoverImage(image)
                return
        if mime.hasUrls():
            for url in mime.urls():
                path = Path(url.toLocalFile())
                if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
                    self._SetCoverSource(path)
                    return
        self.status_label.setText("剪贴板中没有可用图片")

    def _SetCoverImage(self, image: QImage) -> None:
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        data = bytes(buffer.data())
        buffer.close()
        target = self.temp_dir / f"pasted_{int(time.time() * 1000)}.png"
        target.write_bytes(data)
        self._SetCoverSource(target)

    def _SetCoverSource(self, path: Path) -> None:
        self.cover_source = path
        self._remove_requested = False
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self.status_label.setText("无法读取该图片")
            return
        self.preview.setPixmap(
            pixmap.scaled(
                self.preview.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self.status_label.setText(f"已选择：{path.name}")

    def _OnRemove(self) -> None:
        self._remove_requested = True
        self.cover_source = None
        self.preview.setPixmap(QPixmap())
        self.preview.setText("将移除封面")
        self.status_label.setText("确定后将移除范围内所有行的封面")

    # ---- 提取全部封面 -------------------------------------------------
    def _OnExportAll(self) -> None:
        if self._export_worker is not None:
            return
        directory = QFileDialog.getExistingDirectory(self, "选择封面输出目录", "")
        if not directory:
            return
        self._export_worker = CoverExportWorker(
            99, self.ffmpeg_path, self.export_items, directory, self
        )
        self._export_worker.progressChanged.connect(self._OnExportProgress)
        self._export_worker.taskFinished.connect(self._OnExportFinished)
        self._export_worker.finished.connect(self._export_worker.deleteLater)
        self._export_worker.start()
        self._SetExporting(True)
        self.status_label.setText("正在提取全部封面…")

    def _OnExportProgress(self, _thread_id: int, done: int, total: int, name: str) -> None:
        self.status_label.setText(f"正在提取全部封面 {done}/{total}：{name}")

    def _OnExportFinished(self, _thread_id: int, summary: dict) -> None:
        self._export_worker = None
        self._SetExporting(False)
        self.status_label.setText(
            f"导出完成：成功 {summary['ok']}，跳过 {summary['skipped']}，"
            f"失败 {summary['failed']} → {summary['output_dir']}"
        )

    def _SetExporting(self, exporting: bool) -> None:
        for button in self._buttons:
            button.setEnabled(not exporting)

    def _StopExport(self) -> None:
        """终止仍在运行的封面导出线程。

        导出线程以本对话框为父对象，若窗口先被销毁而线程仍在运行，
        Qt 会直接中止进程（QThread: Destroyed while thread is still running）。
        """
        worker = self._export_worker
        if worker is None:
            return
        self._export_worker = None
        worker.RequestCancel()
        worker.wait(5000)

    def closeEvent(self, event) -> None:  # noqa: N802 —— Qt 事件
        self._StopExport()
        super().closeEvent(event)

    def done(self, result: int) -> None:  # noqa: N802 —— Qt 方法
        self._StopExport()
        super().done(result)

    # ---- 结果 ---------------------------------------------------------
    def _OnOk(self) -> None:
        if self._remove_requested:
            self.result_action = "remove"
        elif self.cover_source is not None:
            self.result_action = "set"
        else:
            self.result_action = None
        self.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802 —— Qt 事件
        if event.matches(QKeySequence.StandardKey.Paste):
            self._OnPaste()
        else:
            super().keyPressEvent(event)


class MetadataPage(QWidget):
    """音频元数据编辑主页。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("metadataPage")

        self._rows: list[TrackEdit] = []
        self._checked_fields: set[str] = {
            item.key for item in METADATA_FIELDS if item.default_checked
        }
        self._cover_zoom = "small"
        self._write_mode = "inplace"
        self._ffmpeg_path: str | None = None
        self._ffprobe_path: str | None = None
        self._read_worker: MetadataReadWorker | None = None
        self._workers: list[MetadataWriteWorker] = []
        self._worker_summaries: dict[int, dict] = {}
        self._thread_progress: dict[int, tuple[int, int]] = {}
        self._planned_total = 0
        self._live_statistics = {"total": 0, "ok": 0, "failed": 0, "skipped": 0}
        self._error_logs: list[str] = []
        self._last_output_dirs: list[str] = []
        self._closing = False
        self._cover_temp_dir = tempfile.TemporaryDirectory(prefix="meta_covers_ui_")
        self._cover_temp_path = Path(self._cover_temp_dir.name)

        self._BuildUi()
        self._ConnectSignals()
        self._RebuildTable()
        self._UpdateControls()

    # ---- 界面组装 ----------------------------------------------------
    def _BuildUi(self) -> None:
        self.setAcceptDrops(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.scroll_area = ScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        content = QWidget(self.scroll_area)
        content.setObjectName("metadataContent")
        self.scroll_area.setWidget(content)
        layout.addWidget(self.scroll_area)

        self.content_layout = QVBoxLayout(content)
        self.content_layout.setContentsMargins(30, 22, 30, 26)
        self.content_layout.setSpacing(14)

        self.content_layout.addWidget(TitleLabel("元数据编辑", content))
        self.content_layout.addWidget(
            CaptionLabel(
                "基于 ffprobe / ffmpeg · 导入音频后在表格中编辑元数据、封面与格式，确认后写入",
                content,
            )
        )

        self._BuildInputCard()
        self._BuildParamsCard()
        self._BuildTableCard()
        self._BuildLogCard()
        self.content_layout.addStretch(1)

    def _MakeCard(self, title: str) -> tuple[HeaderCardWidget, QVBoxLayout]:
        card = HeaderCardWidget(self.scroll_area)
        card.setTitle(title)
        body = QWidget(card)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(10)
        card.viewLayout.addWidget(body)
        return card, body_layout

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
        card, layout = self._MakeCard("预览与编辑")
        self.content_layout.addWidget(card)

        top_row = QHBoxLayout()
        self.status_label = CaptionLabel("尚未导入文件", card)
        top_row.addWidget(self.status_label)
        top_row.addStretch(1)
        self.reset_button = PushButton("重置全部修改", card)
        self.confirm_button = PrimaryPushButton("确认修改", card)
        self.cancel_button = PushButton("取消", card)
        self.open_output_button = PushButton("打开输出目录", card)
        self.open_output_button.setToolTip("打开本次写入产出文件所在的目录")
        top_row.addWidget(self.reset_button)
        top_row.addWidget(self.confirm_button)
        top_row.addWidget(self.cancel_button)
        top_row.addWidget(self.open_output_button)
        layout.addLayout(top_row)

        self.table = TableWidget(card)
        self.table.setFixedHeight(400)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(_ROW_HEIGHT_SMALL)
        layout.addWidget(self.table)

        self.progress_bar = ProgressBar(card)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        hint = CaptionLabel(
            "点击列标题统一修改该列；点击单元格只修改该行；"
            ".ext1->.ext2 码率 表示将转换。",
            card,
        )
        layout.addWidget(hint)

    def _BuildLogCard(self) -> None:
        card, layout = self._MakeCard("处理日志")
        self.content_layout.addWidget(card)

        top_row = QHBoxLayout()
        self.log_statistics_label = StrongBodyLabel("总数：0    成功：0    失败：0    跳过：0", card)
        clear_button = PushButton("清空", card)
        clear_button.setFixedWidth(72)
        self.export_error_button = PushButton("导出错误日志", card)
        top_row.addWidget(self.log_statistics_label)
        top_row.addStretch(1)
        top_row.addWidget(self.export_error_button)
        top_row.addWidget(clear_button)
        layout.addLayout(top_row)

        self.log_browser = TextBrowser(card)
        self.log_browser.setMinimumHeight(150)
        self.log_browser.setPlaceholderText("暂无日志")
        layout.addWidget(self.log_browser, 1)

        self.clear_log_button = clear_button
        self._UpdateLogExportButtons()

    def _ConnectSignals(self) -> None:
        self.import_files_button.clicked.connect(self._OnImportFiles)
        self.import_folder_button.clicked.connect(self._OnImportFolder)
        self.field_dropdown.clicked.connect(self._OnFieldMenuClicked)
        self.cover_zoom_seg.currentItemChanged.connect(self._OnCoverZoomChanged)
        self.wav_cover_switch.checkedChanged.connect(self._OnWavCoverToggled)
        self.write_mode_seg.currentItemChanged.connect(self._OnWriteModeChanged)
        self.output_root_browse.clicked.connect(self._OnBrowseOutputRoot)
        self.reset_button.clicked.connect(self._OnResetClicked)
        self.confirm_button.clicked.connect(self._OnConfirmClicked)
        self.cancel_button.clicked.connect(self._OnCancelClicked)
        self.open_output_button.clicked.connect(self._OnOpenOutputDir)
        self.clear_log_button.clicked.connect(self.log_browser.clear)
        self.export_error_button.clicked.connect(self._ExportErrorLog)
        self.table.horizontalHeader().sectionClicked.connect(self._OnHeaderClicked)

    # ---- 拖放 ---------------------------------------------------------
    def dragEnterEvent(self, event) -> None:  # noqa: N802 —— Qt 事件
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 —— Qt 事件
        urls = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        if not urls:
            return
        files = [path for path in urls if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS]
        dirs = [path for path in urls if path.is_dir()]
        if dirs and files:
            self._ShowInfoBar("选择无效", "不能同时拖入文件和文件夹", error=True)
            return
        if dirs:
            if len(dirs) > 1:
                self._ShowInfoBar("选择无效", "一次只能拖入一个文件夹，已使用第一个", warning=True)
            try:
                paths = CollectAudioFiles(dirs[0])
            except MetadataError as exc:
                self._ShowInfoBar("导入失败", str(exc), error=True)
                return
            self._StartImport(paths)
        elif files:
            self._StartImport(files)
        event.acceptProposedAction()

    # ---- 导入 ---------------------------------------------------------
    def _OnImportFiles(self) -> None:
        file_paths, _filter = QFileDialog.getOpenFileNames(
            self,
            "选择音频文件",
            "",
            "音频文件 (*.mp3 *.m4a *.mp4 *.aac *.flac *.ogg *.opus *.wav *.wma *.alac);;所有文件 (*.*)",
        )
        if file_paths:
            self._StartImport([Path(path) for path in file_paths])

    def _OnImportFolder(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择音频文件夹", "")
        if not directory:
            return
        try:
            paths = CollectAudioFiles(directory)
        except MetadataError as exc:
            self._ShowInfoBar("导入失败", str(exc), error=True)
            return
        if not paths:
            self._ShowInfoBar("没有找到音频", "该文件夹内没有找到支持的音频文件", warning=True)
            return
        self._StartImport(paths)

    def _StartImport(self, paths: list[Path]) -> None:
        if self._read_worker is not None or self._workers:
            self._ShowInfoBar("请稍候", "正在读取或写入中", warning=True)
            return
        if not paths:
            return
        ffmpeg = self._EnsureFfmpeg()
        if ffmpeg is None:
            return
        ffprobe = self._EnsureFfprobe()
        if ffprobe is None:
            return

        seen: set[str] = set()
        unique: list[Path] = []
        for path in paths:
            try:
                key = str(path.resolve())
            except OSError:
                key = str(path)
            if key not in seen:
                seen.add(key)
                unique.append(path)

        self._rows.clear()
        self.table.clearContents()
        self.table.setRowCount(0)
        self._error_logs = []
        self._SetStatistics(total=0, ok=0, failed=0, skipped=0)
        self.progress_bar.setRange(0, len(unique))
        self.progress_bar.setValue(0)
        self._AppendLog(LOG_INFO, f"开始导入 {len(unique)} 个音频文件…")

        worker = MetadataReadWorker(1, ffprobe, ffmpeg, unique, self)
        worker.logMessage.connect(self._OnWorkerLog)
        worker.progressChanged.connect(self._OnReadProgress)
        worker.rowLoaded.connect(self._OnReadRowLoaded)
        worker.taskFinished.connect(self._OnReadTaskFinished)
        worker.finished.connect(worker.deleteLater)
        self._read_worker = worker
        worker.start()
        self._UpdateControls()

    def _OnReadRowLoaded(self, _thread_id: int, edit: TrackEdit) -> None:
        self._rows.append(edit)
        self._AppendRow(edit)
        self._UpdateModifiedStatus()

    def _OnReadProgress(self, _thread_id: int, done: int, total: int, name: str) -> None:
        self.progress_bar.setValue(done)
        self.status_label.setText(f"正在导入 {done}/{total}：{name}")

    def _OnReadTaskFinished(self, _thread_id: int, summary: dict) -> None:
        self._read_worker = None
        self._UpdateControls()
        if self._closing:
            return
        self.progress_bar.setValue(self.progress_bar.maximum())
        if summary.get("failed"):
            self._ShowInfoBar(
                "导入完成（部分失败）",
                f"成功 {summary['ok']}，失败 {summary['failed']}",
                warning=True,
            )
        else:
            self._ShowInfoBar("导入完成", f"共读取 {summary['ok']} 个文件")
        self.status_label.setText(f"共 {len(self._rows)} 个文件")
        self._SetDefaultOutputRoot()

    def _SetDefaultOutputRoot(self) -> None:
        if not self._rows:
            return
        parents = {str(row.path.resolve().parent) for row in self._rows}
        if len(parents) == 1:
            self.output_root_line.setText(next(iter(parents)))

    # ---- 表格 ---------------------------------------------------------
    def _Columns(self) -> list[tuple[str, str, str]]:
        """当前列定义：[（表头文本, 类型, 字段 key）]，类型为 format/name/cover/text。"""
        columns: list[tuple[str, str, str]] = [
            ("格式", "format", "format"),
            ("文件名", "name", "name"),
        ]
        if "cover" in self._checked_fields:
            columns.append(("封面", "cover", "cover"))
        for item in TEXT_FIELDS:
            if item.key in self._checked_fields:
                columns.append((item.display_name, "text", item.key))
        return columns

    def _ColumnCount(self) -> int:
        return len(self._Columns())

    def _CoverColumn(self) -> int:
        for index, (_text, kind, _key) in enumerate(self._Columns()):
            if kind == "cover":
                return index
        return -1

    def _ColumnIndexOfField(self, key: str) -> int:
        for index, (_text, _kind, column_key) in enumerate(self._Columns()):
            if column_key == key:
                return index
        return -1

    def _FieldKeyForColumn(self, section: int) -> str | None:
        columns = self._Columns()
        if 0 <= section < len(columns):
            _text, kind, key = columns[section]
            if kind == "text":
                return key
        return None

    def _RowIndexOf(self, row: TrackEdit) -> int:
        """按身份（is）查找行号。

        不能用 list.index：TrackEdit 是 dataclass，相等比较会逐字段比对
        （包括缩略图字节），既慢又可能误判。
        """
        for index, item in enumerate(self._rows):
            if item is row:
                return index
        return -1

    def _RebuildTable(self) -> None:
        self.table.clearContents()
        self.table.setRowCount(0)

        headers = [text for text, _kind, _key in self._Columns()]

        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)

        header = self.table.horizontalHeader()
        for col, (_text, kind, _key) in enumerate(self._Columns()):
            if kind in ("format", "cover"):
                header.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            else:
                header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 128)

        for row in self._rows:
            self._AppendRow(row)
        self._ApplyCoverZoom()

    def _AppendRow(self, row: TrackEdit) -> None:
        row_index = self.table.rowCount()
        self.table.insertRow(row_index)
        self._FillRow(row_index, row)

    def _FillRow(self, row_index: int, row: TrackEdit) -> None:
        format_cell = _ClickableLabel(row.format_display)
        format_cell.setAlignment(Qt.AlignmentFlag.AlignCenter)
        format_cell.setToolTip("点击修改该文件的格式")
        format_cell.clicked.connect(lambda _checked=False, r=row: self._OnFormatCellClicked(r))
        self.table.setCellWidget(row_index, 0, format_cell)

        name_cell = _MarqueeLabel(row.file_name)
        name_cell.setToolTip(row.file_name)
        self.table.setCellWidget(row_index, 1, name_cell)

        col = 2
        if "cover" in self._checked_fields:
            cover_cell = _ClickableLabel()
            cover_cell.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cover_cell.setToolTip("点击修改该文件的封面")
            cover_cell.clicked.connect(lambda _checked=False, r=row: self._OnCoverCellClicked(r))
            self.table.setCellWidget(row_index, col, cover_cell)
            self._UpdateCoverCell(row, cover_cell)
            col += 1

        for item in TEXT_FIELDS:
            if item.key not in self._checked_fields:
                continue
            container = _CellEditor(self.table)
            editor = container.line_edit
            editor.setText(row.edited_values.get(item.key, ""))
            editor.setPlaceholderText(row.original_values.get(item.key, ""))
            editor.setToolTip(item.display_name)
            editor.textEdited.connect(
                lambda value, r=row, key=item.key: self._OnTextEdited(r, key, value)
            )
            self.table.setCellWidget(row_index, col, container)
            col += 1

    def _UpdateRowCells(self, row: TrackEdit) -> None:
        row_index = self._RowIndexOf(row)
        if row_index < 0 or row_index >= self.table.rowCount():
            return
        self._FillRow(row_index, row)
        self._ApplyCoverZoomForRow(row_index, row)

    def _UpdateCoverCell(self, row: TrackEdit, cell: _ClickableLabel) -> None:
        cell.setPixmap(QPixmap())
        cell.setText("")
        pixmap: QPixmap | None = None
        if row.cover_action == "set" and row.cover_source is not None:
            pixmap = QPixmap(str(row.cover_source))
        elif row.thumbnail_bytes:
            loaded = QPixmap()
            loaded.loadFromData(row.thumbnail_bytes)
            if not loaded.isNull():
                pixmap = loaded
        if pixmap is not None and not pixmap.isNull():
            size = self._CoverThumbSize()
            cell.setPixmap(
                pixmap.scaled(
                    size,
                    size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            cell.setText("无")

    def _CoverThumbSize(self) -> int:
        return _COVER_THUMB_LARGE if self._cover_zoom == "large" else _COVER_THUMB_SMALL

    def _ApplyCoverZoom(self) -> None:
        small = self._cover_zoom == "small"
        row_height = _ROW_HEIGHT_SMALL if small else _ROW_HEIGHT_LARGE
        cover_width = _COVER_COL_SMALL if small else _COVER_COL_LARGE
        self.table.verticalHeader().setDefaultSectionSize(row_height)
        for row_index in range(self.table.rowCount()):
            self.table.setRowHeight(row_index, row_height)
        cover_col = self._CoverColumn()
        if cover_col >= 0:
            self.table.setColumnWidth(cover_col, cover_width)
            for row_index in range(self.table.rowCount()):
                cell = self.table.cellWidget(row_index, cover_col)
                if isinstance(cell, _ClickableLabel) and row_index < len(self._rows):
                    self._UpdateCoverCell(self._rows[row_index], cell)

    def _ApplyCoverZoomForRow(self, row_index: int, row: TrackEdit) -> None:
        row_height = _ROW_HEIGHT_SMALL if self._cover_zoom == "small" else _ROW_HEIGHT_LARGE
        self.table.setRowHeight(row_index, row_height)
        cover_col = self._CoverColumn()
        if cover_col >= 0:
            cell = self.table.cellWidget(row_index, cover_col)
            if isinstance(cell, _ClickableLabel):
                self._UpdateCoverCell(row, cell)

    # ---- 字段下拉 -----------------------------------------------------
    def _OnFieldMenuClicked(self) -> None:
        menu = CheckableMenu(parent=self)
        for item in METADATA_FIELDS:
            label = "封面" if item.kind == "cover" else item.display_name
            action = Action(label, menu)
            action.setCheckable(True)
            action.setChecked(item.key in self._checked_fields)
            action.toggled.connect(
                lambda checked, key=item.key: self._OnFieldToggled(key, checked)
            )
            menu.addAction(action)
        pos = self.field_dropdown.mapToGlobal(self.field_dropdown.rect().bottomLeft())
        menu.exec(pos)

    def _OnFieldToggled(self, key: str, checked: bool) -> None:
        if checked:
            self._checked_fields.add(key)
        else:
            self._checked_fields.discard(key)
            if key == "cover":
                for row in self._rows:
                    row.cover_action = None
                    row.cover_source = None
            else:
                for row in self._rows:
                    row.edited_values.pop(key, None)
            self._UpdateModifiedStatus()
        self._UpdateControls()
        self._RebuildTable()

    # ---- 缩放 / 修改方式 ---------------------------------------------
    def _OnCoverZoomChanged(self, key: str) -> None:
        self._cover_zoom = key
        self._ApplyCoverZoom()

    def _OnWriteModeChanged(self, key: str) -> None:
        self._write_mode = key
        self._UpdateControls()

    def _OnWavCoverToggled(self, _checked: bool) -> None:
        self._UpdateControls()

    def _OnBrowseOutputRoot(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "选择输出目录", self.output_root_line.text().strip() or ""
        )
        if directory:
            self.output_root_line.setText(directory)

    # ---- 单元格 / 列头操作 -------------------------------------------
    def _OnHeaderClicked(self, section: int) -> None:
        if section == 0:
            self._OpenFormatDialog(None)
            return
        if section == 1:
            return
        if "cover" in self._checked_fields and section == self._CoverColumn():
            self._OpenCoverDialog(None)
            return
        key = self._FieldKeyForColumn(section)
        if key:
            field = FieldByKey(key)
            if field is not None:
                self._OpenTextDialog(field, None)

    def _OnFormatCellClicked(self, row: TrackEdit) -> None:
        self._OpenFormatDialog(row)

    def _OnCoverCellClicked(self, row: TrackEdit) -> None:
        self._OpenCoverDialog(row)

    def _OnTextEdited(self, row: TrackEdit, key: str, value: str) -> None:
        if value != row.original_values.get(key, ""):
            row.edited_values[key] = value
        else:
            row.edited_values.pop(key, None)
        self._UpdateModifiedStatus()

    def _OpenTextDialog(self, field, scope_row: TrackEdit | None) -> None:
        if not self._rows:
            return
        if scope_row is None:
            scope_text = f"将统一应用到全部 {len(self._rows)} 个文件"
        else:
            scope_text = f"仅修改：{scope_row.file_name}"
        dialog = _TextDialog(self, field, scope_text)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.value is not None:
            rows = [scope_row] if scope_row is not None else self._rows
            for row in rows:
                if row.error:
                    continue
                row.edited_values[field.key] = dialog.value
            col = self._ColumnIndexOfField(field.key)
            if col >= 0:
                for row in rows:
                    row_index = self._RowIndexOf(row)
                    if row_index < 0:
                        continue
                    widget = self.table.cellWidget(row_index, col)
                    if isinstance(widget, _CellEditor):
                        widget.line_edit.blockSignals(True)
                        widget.line_edit.setText(dialog.value)
                        widget.line_edit.blockSignals(False)
            self._UpdateModifiedStatus()

    def _OpenFormatDialog(self, scope_row: TrackEdit | None) -> None:
        if not self._rows:
            return
        if scope_row is None:
            scope_text = f"将统一应用到全部 {len(self._rows)} 个文件"
        else:
            scope_text = f"仅修改：{scope_row.file_name}"
        current_key = scope_row.target_format_key if scope_row is not None else None
        current_bitrate = scope_row.bitrate if scope_row is not None else None
        dialog = _FormatDialog(self, scope_text, current_key, current_bitrate)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.format_key is None:
            return
        rows = [scope_row] if scope_row is not None else self._rows
        for row in rows:
            if row.error:
                continue
            if dialog.format_key == "keep":
                row.target_format_key = None
                row.bitrate = None
            else:
                row.target_format_key = dialog.format_key
                row.bitrate = dialog.bitrate
            row_index = self._RowIndexOf(row)
            if row_index < 0:
                continue
            cell = self.table.cellWidget(row_index, 0)
            if isinstance(cell, _ClickableLabel):
                cell.setText(row.format_display)
        self._UpdateModifiedStatus()

    def _OpenCoverDialog(self, scope_row: TrackEdit | None) -> None:
        if not self._rows:
            return
        ffmpeg = self._EnsureFfmpeg()
        if ffmpeg is None:
            return
        if scope_row is None:
            scope_text = f"将统一应用到全部 {len(self._rows)} 个文件"
        else:
            scope_text = f"仅修改：{scope_row.file_name}"
        export_items = [(row.path, row.cover_codec) for row in self._rows if row.error is None]
        dialog = _CoverDialog(self, scope_text, ffmpeg, export_items, self._cover_temp_path)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.result_action is not None:
            self._ApplyCoverResult(dialog.result_action, dialog.cover_source, scope_row)

    def _ApplyCoverResult(
        self,
        action: str,
        cover_source: Path | None,
        scope_row: TrackEdit | None,
    ) -> None:
        rows = [scope_row] if scope_row is not None else self._rows
        for row in rows:
            if row.error:
                continue
            row.cover_action = action
            if action == "set":
                row.cover_source = cover_source
                if (
                    row.original_ext == ".wav"
                    and row.target_format_key is None
                    and self.wav_cover_switch.isChecked()
                ):
                    target_key = self.wav_target_combo.itemData(
                        self.wav_target_combo.currentIndex()
                    )
                    row.target_format_key = target_key
                    row.bitrate = DEFAULT_BITRATE
            else:
                row.cover_source = None
            self._UpdateRowCells(row)
        self._UpdateModifiedStatus()
        if action == "set" and any(
            row.original_ext == ".wav" for row in rows if row.cover_action == "set"
        ) and self.wav_cover_switch.isChecked():
            self._AppendLog(LOG_INFO, "wav 加封面：已自动设置转换格式（写入时转换）")

    # ---- 确认写入 -----------------------------------------------------
    def _OnConfirmClicked(self) -> None:
        modified = [row for row in self._rows if row.modified]
        if not modified:
            self._ShowInfoBar("没有修改", "表格中没有未确认的修改", warning=True)
            return
        if self._read_worker is not None or self._workers:
            return
        ffmpeg = self._EnsureFfmpeg()
        if ffmpeg is None:
            return

        in_place = self._write_mode == "inplace"
        output_root: str | None = None
        if not in_place:
            text = self.output_root_line.text().strip()
            if not text:
                self._ShowInfoBar("无法开始", "另存为需要先选择输出目录", error=True)
                return
            output_root = text

        for row in modified:
            row.backup = self.backup_switch.isChecked() if in_place else False

        self._error_logs = []
        self._worker_summaries = {}
        self._thread_progress = {}
        self._planned_total = len(modified)
        self.progress_bar.setRange(0, len(modified))
        self.progress_bar.setValue(0)
        self._SetStatistics(total=len(modified), ok=0, failed=0, skipped=0)

        worker = MetadataWriteWorker(
            1,
            ffmpeg,
            modified,
            self.overwrite_switch.isChecked(),
            in_place,
            output_root,
            self,
        )
        worker.logMessage.connect(self._OnWorkerLog)
        worker.progressChanged.connect(self._OnWorkerProgress)
        worker.statisticsChanged.connect(self._OnWorkerStatistics)
        worker.taskFinished.connect(self._OnWorkerTaskFinished)
        worker.finished.connect(lambda w=worker: self._OnWorkerThreadFinished(w))
        worker.finished.connect(worker.deleteLater)
        self._workers = [worker]
        self._AppendLog(LOG_INFO, f"确认修改开始：{len(modified)} 个文件")
        worker.start()
        self._UpdateControls()

    def _OnWorkerProgress(self, _thread_id: int, done: int, _total: int, _name: str) -> None:
        self.progress_bar.setValue(done)
        self.status_label.setText(f"正在写入 {done}/{self._planned_total}")

    def _OnWorkerStatistics(
        self, _thread_id: int, total: int, ok: int, failed: int, skipped: int, _done: int
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
            "cancelled": False,
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
            merged["cancelled"] = merged["cancelled"] or summary["cancelled"]
            merged["error_logs"].extend(summary.get("error_logs", []))
            merged["skipped_logs"].extend(summary.get("skipped_logs", []))
            for directory in summary["output_dirs"]:
                if directory not in seen_dirs:
                    seen_dirs.add(directory)
                    merged["output_dirs"].append(directory)
        return merged

    def _OnAllWorkersFinished(self, summary: dict) -> None:
        if summary["cancelled"]:
            self._ShowInfoBar("写入已取消", "部分文件可能未修改", warning=True)
        elif summary["failed"]:
            self._ShowInfoBar(
                "写入完成（有失败项）",
                f"成功 {summary['ok']}，失败 {summary['failed']}，跳过 {summary['skipped']}",
                warning=True,
            )
        else:
            self._ShowInfoBar(
                "写入完成", f"成功 {summary['ok']}，跳过 {summary['skipped']}"
            )
        self._last_output_dirs = summary.get("output_dirs", [])
        for directory in self._last_output_dirs[:5]:
            self._AppendLog(LOG_INFO, f"输出目录：{directory}")
        self._error_logs = summary.get("error_logs", [])
        self._SetStatistics(
            total=summary["total"],
            ok=summary["ok"],
            failed=summary["failed"],
            skipped=summary["skipped"],
        )
        self._UpdateLogExportButtons()

    def _OnWorkerThreadFinished(self, worker: MetadataWriteWorker) -> None:
        if worker in self._workers:
            self._workers.remove(worker)
        if not self._workers:
            self._worker_summaries = {}
            self._thread_progress = {}
            self._UpdateControls()
            # 写入后重新导入，让表格回到磁盘上的真实状态
            if not self._closing and self._rows:
                self._StartImport([row.path for row in self._rows])

    def _OnCancelClicked(self) -> None:
        if self._workers:
            self.status_label.setText("正在取消…")
            self.cancel_button.setEnabled(False)
            for worker in self._workers:
                worker.RequestCancel()

    def _OnOpenOutputDir(self) -> None:
        opened = 0
        for directory in self._last_output_dirs:
            if opened >= 5:
                self._AppendLog(LOG_WARN, "输出目录较多，已打开前 5 个")
                break
            if Path(directory).is_dir():
                os.startfile(directory)  # noqa: S606 —— 打开资源管理器是用户显式动作
                opened += 1

    # ---- 重置 ---------------------------------------------------------
    def _OnResetClicked(self) -> None:
        if not self._rows:
            return
        for row in self._rows:
            row.edited_values.clear()
            row.cover_action = None
            row.cover_source = None
            row.target_format_key = None
            row.bitrate = None
        self._RebuildTable()
        self._UpdateModifiedStatus()

    # ---- ffmpeg / ffprobe ---------------------------------------------
    def SetFfmpegPath(self, path: str | None) -> None:
        self._ffmpeg_path = path
        self._ffprobe_path = None
        if path:
            try:
                self._ffprobe_path = LocateFfprobe(path)
            except MetadataError:
                self._ffprobe_path = None
            text = f"✓ ffmpeg: {path}"
            if self._ffprobe_path:
                text += f"    ffprobe: {self._ffprobe_path}"
            else:
                text += "    ✗ 未找到 ffprobe"
            self.ffmpeg_status_label.setText(text)
        else:
            self.ffmpeg_status_label.setText("✗ ffmpeg 未就绪，请先在“图片处理”页设置 ffmpeg")
        self._UpdateControls()

    def _EnsureFfmpeg(self) -> str | None:
        if self._ffmpeg_path:
            return self._ffmpeg_path
        try:
            path = LocateFfmpeg(None)
        except ConverterError as exc:
            self._ShowInfoBar("无法开始", str(exc), error=True)
            return None
        self._ffmpeg_path = path
        self.ffmpeg_status_label.setText(f"✓ ffmpeg: {path}")
        return path

    def _EnsureFfprobe(self) -> str | None:
        if self._ffprobe_path:
            return self._ffprobe_path
        try:
            path = LocateFfprobe(self._ffmpeg_path)
        except MetadataError as exc:
            self._ShowInfoBar("无法开始", str(exc), error=True)
            return None
        self._ffprobe_path = path
        if self._ffmpeg_path:
            self.ffmpeg_status_label.setText(
                f"✓ ffmpeg: {self._ffmpeg_path}    ffprobe: {path}"
            )
        return path

    # ---- 状态辅助 -----------------------------------------------------
    def _UpdateControls(self) -> None:
        running = self._read_worker is not None or bool(self._workers)
        has_cover = "cover" in self._checked_fields
        self.import_files_button.setEnabled(not running)
        self.import_folder_button.setEnabled(not running)
        self.field_dropdown.setEnabled(not running)

        self._ShowRow(self.cover_zoom_row, has_cover)
        self.cover_zoom_label.setVisible(has_cover)
        self._ShowRow(self.wav_row, has_cover)
        self.wav_cover_label.setVisible(has_cover)
        self.cover_zoom_seg.setEnabled(not running)
        self.wav_cover_switch.setEnabled(not running)
        self.wav_target_combo.setEnabled(not running and self.wav_cover_switch.isChecked())

        self.write_mode_seg.setEnabled(not running)
        in_place = self._write_mode == "inplace"
        self._ShowRow(self.output_root_row, not in_place)
        self.output_root_label.setVisible(not in_place)
        self.output_root_line.setEnabled(not running)
        self.output_root_browse.setEnabled(not running)
        self.backup_switch.setVisible(in_place)
        self.backup_switch.setEnabled(not running and in_place)
        self.overwrite_switch.setEnabled(not running)

        modified_count = sum(1 for row in self._rows if row.modified)
        self.confirm_button.setEnabled(not running and modified_count > 0)
        self.reset_button.setEnabled(not running and modified_count > 0)
        self.cancel_button.setEnabled(bool(self._workers))
        self.open_output_button.setEnabled(not running and bool(self._last_output_dirs))
        self._UpdateLogExportButtons()

    def _ShowRow(self, row: QHBoxLayout, visible: bool) -> None:
        for index in range(row.count()):
            widget = row.itemAt(index).widget()
            if widget is not None:
                widget.setVisible(visible)

    def _UpdateModifiedStatus(self) -> None:
        if not self._rows:
            self.status_label.setText("尚未导入文件")
            return
        modified_count = sum(1 for row in self._rows if row.modified)
        if modified_count:
            self.status_label.setText(
                f"共 {len(self._rows)} 个文件 · 已修改 {modified_count} 个（未确认）"
            )
        else:
            self.status_label.setText(f"共 {len(self._rows)} 个文件")
        self._UpdateControls()

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

    # ---- 日志 ---------------------------------------------------------
    def _OnWorkerLog(self, _thread_id: int, level: int, text: str) -> None:
        self._AppendLog(level, text)

    def _AppendLog(self, level: int, text: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        body = (
            f"[{timestamp}] {_LEVEL_MARKS.get(level, '')}"
            f"{html.escape(text).replace(chr(10), '<br>')}"
        )
        if level == LOG_INFO:
            line = f"<div style='margin:0'>{body}</div>"
        else:
            light, dark = _LOG_COLORS[level]
            color = dark if isDarkTheme() else light
            line = f"<div style='margin:0;color:{color}'>{body}</div>"
        cursor = self.log_browser.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertHtml(line)
        self.log_browser.setTextCursor(cursor)
        self.log_browser.ensureCursorVisible()

    def _ExportErrorLog(self) -> None:
        if not self._error_logs or self._workers:
            return
        file_path, _filter = QFileDialog.getSaveFileName(
            self, "导出错误日志", "error_log.txt", "文本文件 (*.txt);;所有文件 (*.*)"
        )
        if not file_path:
            return
        try:
            Path(file_path).write_text("\n".join(self._error_logs) + "\n", encoding="utf-8")
        except OSError as exc:
            self._ShowInfoBar("导出失败", str(exc), error=True)
            return
        self._ShowInfoBar("导出成功", f"日志已保存到：{file_path}")

    def _ShowInfoBar(
        self, title: str, content: str, error: bool = False, warning: bool = False
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
        self._closing = True
        if self._read_worker is not None:
            self._read_worker.RequestCancel()
            self._read_worker.wait()
            self._read_worker = None
        for worker in list(self._workers):
            worker.RequestCancel()
        for worker in list(self._workers):
            worker.wait()
        self._workers.clear()
        self._cover_temp_dir.cleanup()
