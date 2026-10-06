"""图片后台任务：Qt 线程和信号适配，执行由应用层负责。"""
from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal

from app.image.planner import build_image_task
from app.image.models import Batch, ConvertOptions
from app.ffmpeg_environment import FfmpegCapabilities
from app.task_runner import TaskRunner
from app.task_models import LOG_ERROR, TaskResult


class ConvertWorker(QThread):
    logMessage = pyqtSignal(int, int, str)
    progressChanged = pyqtSignal(int, int, int, str)
    statisticsChanged = pyqtSignal(int, int, int, int, int, int)
    taskFinished = pyqtSignal(int, object)

    def __init__(self, thread_id: int, batches: list[Batch], options: ConvertOptions, capabilities: FfmpegCapabilities, parent=None):
        super().__init__(parent)
        self._thread_id = thread_id
        self._batches = batches
        self._options = options
        self._capabilities = capabilities
        self._runner = TaskRunner(
            on_log=lambda event: self.logMessage.emit(thread_id, event.level, event.message),
            on_progress=lambda progress: self.progressChanged.emit(
                thread_id, progress.done, progress.total, progress.current_file),
            on_statistics=lambda stats: self.statisticsChanged.emit(
                thread_id, stats.total, stats.ok, stats.failed, stats.skipped, stats.done),
        )

    def RequestCancel(self) -> None:
        self._runner.request_cancel()

    def RequestFinishAfterCurrentBatch(self) -> None:
        self._runner.request_finish_after_current_batch()

    def run(self) -> None:
        try:
            plan = build_image_task(self._batches, self._options, self._capabilities)
            result = self._runner.run(plan)
        except Exception as exc:
            message = f"任务异常：{exc}"
            self.logMessage.emit(self._thread_id, LOG_ERROR, message)
            result = TaskResult(total=sum(len(batch.files) for batch in self._batches), failed=1, error_logs=[message])
        self.taskFinished.emit(self._thread_id, result)
