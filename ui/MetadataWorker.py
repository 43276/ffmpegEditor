"""音频后台任务：Qt 线程和信号适配，业务执行在 app。"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from threading import Event

from PyQt6.QtCore import QThread, pyqtSignal

from app.audio.reader import read_audio_edits
from app.audio.metadata_planner import build_metadata_task
from app.audio.cover_planner import build_cover_export_task
from app.audio.models import TrackEdit
from app.task_runner import TaskRunner
from app.task_models import LOG_ERROR, TaskResult


class MetadataReadWorker(QThread):
    logMessage = pyqtSignal(int, int, str)
    progressChanged = pyqtSignal(int, int, int, str)
    rowLoaded = pyqtSignal(int, object)
    taskFinished = pyqtSignal(int, object)

    def __init__(self, thread_id: int, ffprobe_path: str, ffmpeg_path: str, files: list[Path], parent=None):
        super().__init__(parent)
        self._thread_id = thread_id
        self._ffprobe_path = ffprobe_path
        self._ffmpeg_path = ffmpeg_path
        self._files = files
        self._cancel = Event()

    def RequestCancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        result = read_audio_edits(
            self._ffprobe_path, self._ffmpeg_path, self._files,
            cancel_check=self._cancel.is_set,
            on_row=lambda edit: self.rowLoaded.emit(self._thread_id, edit),
            on_log=lambda event: self.logMessage.emit(self._thread_id, event.level, event.message),
            on_progress=lambda progress: self.progressChanged.emit(
                self._thread_id, progress.done, progress.total, progress.current_file),
        )
        self.taskFinished.emit(self._thread_id, result)


class MetadataWriteWorker(QThread):
    logMessage = pyqtSignal(int, int, str)
    progressChanged = pyqtSignal(int, int, int, str)
    statisticsChanged = pyqtSignal(int, int, int, int, int, int)
    taskFinished = pyqtSignal(int, object)

    def __init__(self, thread_id: int, ffmpeg_path: str, edits: list[TrackEdit], overwrite: bool, in_place: bool, output_root: str | None, parent=None):
        super().__init__(parent)
        self._thread_id = thread_id
        self._ffmpeg_path = ffmpeg_path
        self._edits = [replace(edit, original_values=dict(edit.original_values), edited_values=dict(edit.edited_values)) for edit in edits]
        self._overwrite = overwrite
        self._in_place = in_place
        self._output_root = output_root
        self._runner = TaskRunner(
            on_log=lambda event: self.logMessage.emit(thread_id, event.level, event.message),
            on_progress=lambda progress: self.progressChanged.emit(
                thread_id, progress.done, progress.total, progress.current_file),
            on_statistics=lambda stats: self.statisticsChanged.emit(
                thread_id, stats.total, stats.ok, stats.failed, stats.skipped, stats.done),
        )

    def RequestCancel(self) -> None:
        self._runner.request_cancel()

    def run(self) -> None:
        try:
            plan = build_metadata_task(self._ffmpeg_path, self._edits, self._overwrite, self._in_place, self._output_root)
            result = self._runner.run(plan)
        except Exception as exc:
            message = f"任务异常：{exc}"
            self.logMessage.emit(self._thread_id, LOG_ERROR, message)
            result = TaskResult(total=len(self._edits), failed=1, error_logs=[message])
        self.taskFinished.emit(self._thread_id, result)


class CoverExportWorker(QThread):
    logMessage = pyqtSignal(int, int, str)
    progressChanged = pyqtSignal(int, int, int, str)
    taskFinished = pyqtSignal(int, object)

    def __init__(self, thread_id: int, ffmpeg_path: str, items: list[tuple[Path, str | None]], output_dir: str, parent=None):
        super().__init__(parent)
        self._thread_id = thread_id
        self._ffmpeg_path = ffmpeg_path
        self._items = items
        self._output_dir = output_dir
        self._runner = TaskRunner(
            on_log=lambda event: self.logMessage.emit(thread_id, event.level, event.message),
            on_progress=lambda progress: self.progressChanged.emit(
                thread_id, progress.done, progress.total, progress.current_file),
        )

    def RequestCancel(self) -> None:
        self._runner.request_cancel()

    def run(self) -> None:
        try:
            plan = build_cover_export_task(self._ffmpeg_path, self._items, self._output_dir)
            result = self._runner.run(plan)
        except Exception as exc:
            message = f"任务异常：{exc}"
            self.logMessage.emit(self._thread_id, LOG_ERROR, message)
            result = TaskResult(total=len(self._items), failed=1, error_logs=[message])
        self.taskFinished.emit(self._thread_id, result)
