"""管理单个活动任务；仅在线程真实结束后释放引用并允许重启。"""
from __future__ import annotations

from dataclasses import replace
from enum import Enum

from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot

from app.task_models import TaskResult
from .worker import TaskOperation, TaskWorker


class TaskState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    CANCELLING = "cancelling"
    FINISHING = "finishing"
    FINALIZING = "finalizing"


class TaskController(QObject):
    logMessage = pyqtSignal(object)
    progressChanged = pyqtSignal(object)
    statisticsChanged = pyqtSignal(object)
    rowLoaded = pyqtSignal(object)
    resultReady = pyqtSignal(object)
    completed = pyqtSignal(str, object)
    stateChanged = pyqtSignal(object)
    idle = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker: TaskWorker | None = None
        self._task_id = 0
        self._kind = ""
        self._state = TaskState.IDLE
        self._result: TaskResult | None = None
        self._supports_finish = False
        self._closing = False

    @property
    def worker(self) -> TaskWorker | None:
        return self._worker

    @property
    def task_id(self) -> int:
        return self._task_id

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def state(self) -> TaskState:
        return self._state

    @property
    def active(self) -> bool:
        return self._worker is not None

    @property
    def can_cancel(self) -> bool:
        return self._state in (TaskState.RUNNING, TaskState.FINISHING)

    @property
    def can_finish(self) -> bool:
        return self._supports_finish and self._state == TaskState.RUNNING

    def _set_state(self, state: TaskState) -> None:
        if state != self._state:
            self._state = state
            self.stateChanged.emit(state)

    def start(self, operation: TaskOperation, *, total: int = 0,
              kind: str = "convert", supports_finish: bool = False) -> bool:
        if self.active or self._closing:
            return False
        self._task_id += 1
        self._kind = kind
        self._result = None
        self._supports_finish = supports_finish
        worker = TaskWorker(self._task_id, operation, total, self)
        self._worker = worker
        queued = Qt.ConnectionType.QueuedConnection
        worker.logMessage.connect(self._on_log, queued)
        worker.progressChanged.connect(self._on_progress, queued)
        worker.statisticsChanged.connect(self._on_statistics, queued)
        worker.rowLoaded.connect(self._on_row, queued)
        worker.taskFinished.connect(self._on_result, queued)
        worker.finished.connect(self._on_thread_finished, queued)
        self._set_state(TaskState.RUNNING)
        worker.start()
        return True

    def _accepts(self, task_id: int) -> bool:
        return self.active and task_id == self._task_id and self._result is None

    @pyqtSlot(int, object)
    def _on_log(self, task_id: int, event) -> None:
        if self._accepts(task_id):
            self.logMessage.emit(event)

    @pyqtSlot(int, object)
    def _on_progress(self, task_id: int, progress) -> None:
        if self._accepts(task_id):
            self.progressChanged.emit(progress)

    @pyqtSlot(int, object)
    def _on_statistics(self, task_id: int, statistics) -> None:
        if self._accepts(task_id):
            self.statisticsChanged.emit(statistics)

    @pyqtSlot(int, object)
    def _on_row(self, task_id: int, row) -> None:
        if self._accepts(task_id):
            self.rowLoaded.emit(row)

    @pyqtSlot(int, object)
    def _on_result(self, task_id: int, result: TaskResult) -> None:
        if self._accepts(task_id):
            # Worker 发出结果与 GUI 消费信号之间仍可收到有效停止请求。
            if self._state == TaskState.CANCELLING:
                result = replace(result, cancelled=True)
            elif self._state == TaskState.FINISHING and not result.cancelled:
                result = replace(result, early_stopped=True)
            self._result = result
            self._set_state(TaskState.FINALIZING)
            self.resultReady.emit(result)

    @pyqtSlot()
    def _on_thread_finished(self) -> None:
        worker = self.sender()
        if worker is not self._worker:
            return
        kind = self._kind
        result = self._result or worker.result
        if result is None:
            result = TaskResult(failed=1, error_logs=["线程未返回任务结果"])
        self._worker = None
        worker.deleteLater()
        self._set_state(TaskState.IDLE)
        self.completed.emit(kind, result)
        # completed 的接收方可以启动后续读取；此时不能宣布空闲。
        if not self.active:
            self.idle.emit()

    def request_cancel(self) -> bool:
        if not self.can_cancel:
            return False
        self._worker.request_cancel()
        self._set_state(TaskState.CANCELLING)
        return True

    def request_finish_after_current_batch(self) -> bool:
        if not self.can_finish:
            return False
        self._worker.request_finish_after_current_batch()
        self._set_state(TaskState.FINISHING)
        return True

    def shutdown(self) -> None:
        self._closing = True
        if self.active:
            self.request_cancel()
        else:
            self.idle.emit()
