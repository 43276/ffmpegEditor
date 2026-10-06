"""把应用层回调转为带任务编号的 Qt 信号。"""
from __future__ import annotations

from dataclasses import dataclass
from threading import Event
from typing import Callable

from PyQt6.QtCore import QThread, pyqtSignal

from app.task_models import LogEvent, LogLevel, TaskProgress, TaskResult, TaskStatistics


@dataclass(frozen=True)
class TaskContext:
    cancel_check: Callable[[], bool]
    finish_check: Callable[[], bool]
    on_log: Callable[[LogEvent], None]
    on_progress: Callable[[TaskProgress], None]
    on_statistics: Callable[[TaskStatistics], None]
    on_row: Callable[[object], None]


TaskOperation = Callable[[TaskContext], TaskResult]


class TaskWorker(QThread):
    logMessage = pyqtSignal(int, object)
    progressChanged = pyqtSignal(int, object)
    statisticsChanged = pyqtSignal(int, object)
    rowLoaded = pyqtSignal(int, object)
    taskFinished = pyqtSignal(int, object)

    def __init__(self, task_id: int, operation: TaskOperation, total: int = 0, parent=None):
        super().__init__(parent)
        self.task_id = task_id
        self.result: TaskResult | None = None
        self._operation = operation
        self._total = total
        self._cancel = Event()
        self._finish = Event()

    def request_cancel(self) -> None:
        self._cancel.set()

    def request_finish_after_current_batch(self) -> None:
        self._finish.set()

    def run(self) -> None:
        context = TaskContext(
            self._cancel.is_set, self._finish.is_set,
            lambda event: self.logMessage.emit(self.task_id, event),
            lambda progress: self.progressChanged.emit(self.task_id, progress),
            lambda statistics: self.statisticsChanged.emit(self.task_id, statistics),
            lambda row: self.rowLoaded.emit(self.task_id, row),
        )
        try:
            if self._cancel.is_set():
                self.result = TaskResult(total=self._total, cancelled=True)
            else:
                self.result = self._operation(context)
        except Exception as exc:
            message = f"任务异常：{exc}"
            context.on_log(LogEvent(LogLevel.ERROR, message))
            self.result = TaskResult(total=self._total, failed=1,
                                     cancelled=self._cancel.is_set(), error_logs=[message])
        self.taskFinished.emit(self.task_id, self.result)
