"""元数据编辑输入、封面预览与安全的异步导出弹窗。"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from PyQt6.QtCore import QBuffer, QIODevice, Qt, pyqtSignal
from PyQt6.QtGui import QImage, QKeySequence, QPixmap
from PyQt6.QtWidgets import QApplication, QDialog, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout
from qfluentwidgets import CaptionLabel, ComboBox, LineEdit, PrimaryPushButton, PushButton, StrongBodyLabel

from app.audio.formats import IMAGE_EXTENSIONS, DEFAULT_BITRATE, FORMAT_OPTIONS, format_by_key as FormatByKey
from app.task_models import TaskProgress, TaskResult
from ui.tasks.controller import TaskController
from ui.tasks.jobs import cover_export_job

class TextDialog(QDialog):
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


class FormatDialog(QDialog):
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


class CoverDialog(QDialog):
    """封面小窗：按当前范围选择、粘贴或移除封面。"""

    idle = pyqtSignal()

    def __init__(self, parent, scope_text: str, ffmpeg_path: str, export_items, resources):
        super().__init__(parent)
        self.setWindowTitle("封面")
        self.setModal(True)
        self.setMinimumWidth(480)
        self.ffmpeg_path = ffmpeg_path
        self.export_items = export_items
        self._resources = resources if hasattr(resources, "store_png") else None
        if self._resources is None and parent is not None and hasattr(parent, "_controller"):
            owner = parent._controller.resources
            if Path(resources) == owner.path:
                self._resources = owner
        self.temp_dir = self._resources.path if self._resources is not None else Path(resources)
        if self._resources is not None:
            owner = self._resources
            token = owner.acquire()
            self.finished.connect(lambda _result: owner.release(token))
            self.destroyed.connect(lambda: owner.release(token))
        self.result_action: str | None = None   # "set" | "remove" | None
        self.cover_source: Path | None = None
        self._remove_requested = False
        self._export_controller = TaskController(self)
        self._pending_done: int | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        layout.addWidget(CaptionLabel(scope_text, self))

        button_row = QHBoxLayout()
        choose_button = PushButton("选择图片文件…", self)
        paste_button = PushButton("粘贴图片", self)
        remove_button = PushButton("移除封面", self)
        button_row.addWidget(choose_button)
        button_row.addWidget(paste_button)
        export_button = None
        if export_items is not None:
            export_button = PushButton("提取全部封面…", self)
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
        if export_items is not None:
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

        self._buttons = [choose_button, paste_button, remove_button, ok_button]
        if export_button is not None:
            self._buttons.append(export_button)

        choose_button.clicked.connect(self._OnChoose)
        paste_button.clicked.connect(self._OnPaste)
        if export_button is not None:
            export_button.clicked.connect(self._OnExportAll)
        remove_button.clicked.connect(self._OnRemove)
        ok_button.clicked.connect(self._OnOk)
        cancel_button.clicked.connect(self.reject)
        self._export_controller.progressChanged.connect(self._OnExportProgress)
        self._export_controller.completed.connect(self._OnExportFinished)
        self._export_controller.stateChanged.connect(
            lambda _state: self._SetExporting(self._export_controller.active))
        self._export_controller.idle.connect(self._FinishPendingClose)
        self._export_controller.idle.connect(self.idle)

    @property
    def active(self) -> bool:
        return self._export_controller.active

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
        if self.active or self._pending_done is not None:
            return
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
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, "PNG"):
            buffer.close()
            self.status_label.setText("无法编码剪贴板图片")
            return
        data = bytes(buffer.data())
        buffer.close()
        try:
            if self._resources is not None:
                target = self._resources.store_png(data)
            else:
                self.temp_dir.mkdir(parents=True, exist_ok=True)
                target = self.temp_dir / f"pasted_{uuid4().hex}.png"
                target.write_bytes(data)
        except OSError as exc:
            self.status_label.setText(f"保存剪贴板图片失败：{exc}")
            return
        self._SetCoverSource(target)

    def _SetCoverSource(self, path: Path) -> None:
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self.status_label.setText("无法读取该图片")
            return
        self.cover_source = path
        self._remove_requested = False
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
        if self.active or self._pending_done is not None or self.export_items is None:
            return
        directory = QFileDialog.getExistingDirectory(self, "选择封面输出目录", "")
        if not directory:
            return
        self._export_controller.start(cover_export_job(self.ffmpeg_path, self.export_items, directory),
                                      total=len(self.export_items), kind="export")
        self._SetExporting(True)
        self.status_label.setText("正在提取全部封面…")

    def _OnExportProgress(self, progress: TaskProgress) -> None:
        if self._pending_done is None:
            self.status_label.setText(
                f"正在提取全部封面 {progress.done}/{progress.total}：{progress.current_file}")

    def _OnExportFinished(self, _kind: str, summary: TaskResult) -> None:
        if self._pending_done is not None:
            return
        self._SetExporting(False)
        self.status_label.setText(
            f"{'导出已取消' if summary.cancelled else '导出完成'}：成功 {summary.ok}，跳过 {summary.skipped}，"
            f"失败 {summary.failed} → {summary.output_dir}"
        )

    def _SetExporting(self, exporting: bool) -> None:
        for button in self._buttons:
            button.setEnabled(not exporting)

    def _FinishPendingClose(self) -> None:
        if self._pending_done is not None:
            result = self._pending_done
            self._pending_done = None
            super().done(result)

    def Shutdown(self) -> None:
        self.reject()

    def closeEvent(self, event) -> None:  # noqa: N802 —— Qt 事件
        if self.active:
            event.ignore()
            self.done(QDialog.DialogCode.Rejected)
        else:
            super().closeEvent(event)

    def done(self, result: int) -> None:  # noqa: N802 —— Qt 方法
        if self.active:
            self._pending_done = result
            self._SetExporting(True)
            self.status_label.setText("正在取消导出，等待线程结束…")
            self._export_controller.shutdown()
            return
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
        if (event.matches(QKeySequence.StandardKey.Paste) and not self.active
                and self._pending_done is None):
            self._OnPaste()
        else:
            super().keyPressEvent(event)
