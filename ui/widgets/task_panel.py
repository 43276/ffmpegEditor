"""任务能力决定按钮，控制器状态决定可用性和统一进度展示。"""
from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, HeaderCardWidget, PrimaryPushButton, ProgressBar, PushButton

from .log_panel import statistics_text


class TaskPanel(HeaderCardWidget):
    def __init__(self, parent=None, start_text="开始处理", supports_cancel=True,
                 supports_finish=True, finish_tooltip="", show_output=True, show_exports=True):
        super().__init__(parent)
        self.setTitle("任务")
        body = QWidget(self)
        self.body_layout = QVBoxLayout(body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(10)
        self.viewLayout.addWidget(body)
        self.button_row = QHBoxLayout()
        self.start_button = PrimaryPushButton(start_text, self)
        self.cancel_button = PushButton("取消", self)
        self.end_button = PushButton("结束", self)
        self.end_button.setToolTip(finish_tooltip)
        self.open_folder_button = PushButton("打开输出目录", self)
        self.export_error_button = PushButton("导出错误日志", self)
        self.export_skipped_button = PushButton("导出跳过日志", self)
        self._supports_cancel = supports_cancel
        self._supports_finish = supports_finish
        for button, visible in (
            (self.start_button, True), (self.cancel_button, supports_cancel),
            (self.end_button, supports_finish), (self.open_folder_button, show_output),
            (self.export_error_button, show_exports), (self.export_skipped_button, show_exports),
        ):
            self.button_row.addWidget(button)
            button.setVisible(visible)
        self.button_row.addStretch(1)
        self.body_layout.addLayout(self.button_row)
        self.progress_bar = ProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.body_layout.addWidget(self.progress_bar)
        self.status_label = CaptionLabel("就绪", self)
        self.body_layout.addWidget(self.status_label)
        self.statistics_label = CaptionLabel("", self)
        self.statistics_label.hide()
        self.body_layout.addWidget(self.statistics_label)

    def update_state(self, controller, ready, closing=False, has_output=False, has_errors=False, has_skipped=False):
        busy = controller.active or closing
        self.start_button.setEnabled(bool(ready) and not busy)
        self.cancel_button.setEnabled(self._supports_cancel and controller.can_cancel and not closing)
        self.end_button.setEnabled(self._supports_finish and controller.can_finish and not closing)
        self.open_folder_button.setEnabled(not busy and has_output)
        self.export_error_button.setEnabled(not busy and has_errors)
        self.export_skipped_button.setEnabled(not busy and has_skipped)

    def set_progress(self, progress, show_file=False):
        self.progress_bar.setRange(0, max(1, progress.total))
        self.progress_bar.setValue(progress.done)
        suffix = f"：{progress.current_file}" if show_file and progress.current_file else ""
        self.status_label.setText(f"进度 {progress.done}/{progress.total}{suffix}")

    def set_statistics(self, stats):
        self.statistics_label.setText(statistics_text(stats))

    def set_result(self, result, action_text="处理", boundary="文件夹"):
        if result.cancelled:
            text = "已取消"
        elif result.early_stopped:
            text = f"已结束：当前{boundary}已完成"
        elif result.failed:
            text = f"{action_text}完成：成功 {result.ok}，失败 {result.failed}，跳过 {result.skipped}"
        else:
            text = f"{action_text}完成：成功 {result.ok}，跳过 {result.skipped}"
        self.status_label.setText(text)
        self.set_statistics(result)
