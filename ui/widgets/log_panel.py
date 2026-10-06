"""主题感知日志：转义、时间戳、末尾滚动、统计与 UTF-8 导出。"""
from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import QFileDialog, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, HeaderCardWidget, PushButton, StrongBodyLabel, TextBrowser
from qfluentwidgets.common.style_sheet import isDarkTheme
from qfluentwidgets import qconfig

from app.task_models import LogLevel


_COLORS = {
    LogLevel.OK: ("#0f7b0f", "#7adfa0"),
    LogLevel.WARN: ("#9a6700", "#f5c26b"),
    LogLevel.ERROR: ("#c42b1c", "#ff9aa2"),
}
_MARKS = {LogLevel.OK: "✓ ", LogLevel.WARN: "⚠ ", LogLevel.ERROR: "✗ "}


def statistics_text(stats) -> str:
    return f"总数：{stats.total}    成功：{stats.ok}    失败：{stats.failed}    跳过：{stats.skipped}"


class LogPanel(HeaderCardWidget):
    def __init__(self, parent=None, title="处理日志", hint="转换过程与 ffmpeg 输出", minimum_height=150):
        super().__init__(parent)
        self.setTitle(title)
        self._records = []
        self._rendering = False
        body = QWidget(self)
        self.body_layout = QVBoxLayout(body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(10)
        self.viewLayout.addWidget(body)
        top = QHBoxLayout()
        self.statistics_label = StrongBodyLabel("总数：0    成功：0    失败：0    跳过：0", self)
        top.addWidget(self.statistics_label)
        if hint:
            top.addSpacing(16)
            top.addWidget(CaptionLabel(hint, self))
        top.addStretch(1)
        self.clear_button = PushButton("清空", self)
        self.clear_button.setFixedWidth(72)
        top.addWidget(self.clear_button)
        self.body_layout.addLayout(top)
        self.browser = TextBrowser(self)
        self.browser.setMinimumHeight(minimum_height)
        self.browser.setPlaceholderText("暂无日志")
        self.body_layout.addWidget(self.browser, 1)
        self.clear_button.clicked.connect(self.clear)
        self.browser.textChanged.connect(self._on_text_changed)
        qconfig.themeChanged.connect(self._render_history)

    def clear(self):
        self._records.clear()
        self.browser.clear()

    def append(self, level: LogLevel, text: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self._records.append((timestamp, level, str(text)))
        self._insert_record(timestamp, level, text)

    def _on_text_changed(self):
        # 同时支持页面现有的 browser.clear() 调用。
        if not self._rendering and self.browser.document().isEmpty():
            self._records.clear()

    def _render_history(self, *_args):
        scrollbar = self.browser.verticalScrollBar()
        position = scrollbar.value()
        at_end = position >= scrollbar.maximum()
        self._rendering = True
        try:
            self.browser.clear()
            for timestamp, level, message in self._records:
                self._insert_record(timestamp, level, message)
        finally:
            self._rendering = False
        scrollbar.setValue(scrollbar.maximum() if at_end else position)

    def _insert_record(self, timestamp, level, text):
        body = f"[{timestamp}] {_MARKS.get(level, '')}{html.escape(str(text)).replace(chr(10), '<br>')}"
        colors = _COLORS.get(level)
        style = "margin:0"
        if colors:
            style += f";color:{colors[1] if isDarkTheme() else colors[0]}"
        cursor = self.browser.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertHtml(f"<div style='{style}'>{body}</div><br>")
        self.browser.setTextCursor(cursor)
        self.browser.ensureCursorVisible()

    def set_statistics(self, stats):
        self.statistics_label.setText(statistics_text(stats))

    @staticmethod
    def export_entries(parent, title: str, default_name: str, entries, on_notice):
        if not entries:
            return
        path, _ = QFileDialog.getSaveFileName(parent, title, default_name, "文本文件 (*.txt);;所有文件 (*.*)")
        if not path:
            return
        try:
            Path(path).write_text("\n".join(entries) + "\n", encoding="utf-8")
        except OSError as exc:
            on_notice("导出失败", str(exc), error=True)
            return
        on_notice("导出成功", f"日志已保存到：{path}")
