"""视频后台任务：Qt 线程和信号适配，执行由应用层负责。"""
from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal

from app.video.planner import build_video_task
from app.video.models import VideoBatch, VideoCompressOptions
from app.ffmpeg_environment import FfmpegCapabilities
from app.task_runner import TaskRunner
from app.task_models import LOG_ERROR, TaskResult


class VideoCompressWorker(QThread):
    logMessage = pyqtSignal(int, str)
    progressChanged = pyqtSignal(int, int, str)
    taskFinished = pyqtSignal(object)

    def __init__(self, batches: list[VideoBatch], options: VideoCompressOptions, capabilities: FfmpegCapabilities, parent=None):
        super().__init__(parent)
        self._batches = batches
        self._options = options
        self._capabilities = capabilities
        self._runner = TaskRunner(
            on_log=lambda event: self.logMessage.emit(event.level, event.message),
            on_progress=lambda progress: self.progressChanged.emit(
                progress.done, progress.total, progress.current_file),
        )

    def RequestCancel(self) -> None:
        self._runner.request_cancel()

    def RequestFinishAfterCurrentBatch(self) -> None:
        self._runner.request_finish_after_current_batch()

    def run(self) -> None:
        try:
            plan = build_video_task(self._batches, self._options, self._capabilities)
            result = self._runner.run(plan)
        except Exception as exc:
            message = f"任务异常：{exc}"
            self.logMessage.emit(LOG_ERROR, message)
            result = TaskResult(total=sum(len(batch.files) for batch in self._batches),
                                failed=1, error_logs=[message])
        self.taskFinished.emit(result)
