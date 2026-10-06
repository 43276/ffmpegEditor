"""专辑后台任务：Qt 线程和信号适配，执行由应用层负责。"""
from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal

from app.audio.album_planner import build_album_task
from app.audio.models import AlbumPlan
from app.task_runner import TaskRunner
from app.task_models import LOG_ERROR, TaskResult


class AlbumWorker(QThread):
    logMessage = pyqtSignal(int, int, str)
    progressChanged = pyqtSignal(int, int, int, str)
    statisticsChanged = pyqtSignal(int, int, int, int, int, int)
    taskFinished = pyqtSignal(int, object)

    def __init__(self, thread_id: int, plan: AlbumPlan, overwrite: bool, ffmpeg_path: str, parent=None):
        super().__init__(parent)
        self._thread_id = thread_id
        self._plan = plan
        self._overwrite = overwrite
        self._ffmpeg_path = ffmpeg_path
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
            plan = build_album_task(self._plan, self._ffmpeg_path, self._overwrite)
            result = self._runner.run(plan)
        except Exception as exc:
            message = f"任务异常：{exc}"
            self.logMessage.emit(self._thread_id, LOG_ERROR, message)
            result = TaskResult(total=self._plan.file_count, failed=1, error_logs=[message])
        self.taskFinished.emit(self._thread_id, result)
