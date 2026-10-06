"""视频压缩后台线程。"""
from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal

from app.Converter import ConverterError, RunFileProcess
from app.VideoCore import BuildVideoCompressCommand, OutputVideoName
from app.video.models import VideoBatch, VideoCompressOptions
from app.ffmpeg_environment import FfmpegCapabilities
from app.output_files import (
    commit_output, ensure_output_directory, output_transaction,
    path_key, reserve_output_path, temporary_path_for,
)
from app.task_models import (
    LOG_ERROR, LOG_INFO, LOG_OK, LOG_WARN, FilePlan, TaskProgress, TaskResult,
)


class VideoCompressWorker(QThread):
    logMessage = pyqtSignal(int, str)
    progressChanged = pyqtSignal(int, int, str)
    taskFinished = pyqtSignal(object)

    def __init__(self, batches: list[VideoBatch], options: VideoCompressOptions, capabilities: FfmpegCapabilities, parent=None):
        super().__init__(parent)
        self._batches = batches
        self._options = options
        self._capabilities = capabilities
        self._cancel_requested = False
        self._finish_requested = False

    def RequestCancel(self) -> None:
        self._cancel_requested = True

    def RequestFinishAfterCurrentBatch(self) -> None:
        self._finish_requested = True

    def _Progress(self, done: int, total: int, current_file: str) -> None:
        progress = TaskProgress(done, total, current_file)
        self.progressChanged.emit(progress.done, progress.total, progress.current_file)

    def run(self) -> None:
        total = sum(len(batch.files) for batch in self._batches)
        done = ok = failed = skipped = 0
        cancelled = early_stopped = False
        used_outputs = {path_key(source) for batch in self._batches for source in batch.files}
        output_dirs: list[str] = []
        self.logMessage.emit(LOG_INFO, f"任务开始：{len(self._batches)} 个批次、{total} 个视频")
        try:
            for batch in self._batches:
                if self._cancel_requested:
                    cancelled = True
                    break
                if self._finish_requested:
                    early_stopped = True
                    break
                ensure_output_directory(batch.output_dir)
                output_dirs.append(str(batch.output_dir))
                for source in batch.files:
                    if self._cancel_requested:
                        cancelled = True
                        break
                    destination = reserve_output_path(batch.output_dir / OutputVideoName(source), used_outputs)
                    if destination.exists() and not self._options.overwrite:
                        done += 1
                        skipped += 1
                        self.logMessage.emit(LOG_WARN, f"已跳过（输出已存在）：{destination.name}")
                        self._Progress(done, total, source.name)
                        continue
                    done += 1
                    temporary = temporary_path_for(destination)
                    try:
                        command = BuildVideoCompressCommand(self._options, self._capabilities, source)
                        command.append(str(temporary))
                        plan = FilePlan(source, destination, temporary, tuple(command),
                                        overwrite=self._options.overwrite)
                        with output_transaction(plan):
                            result = RunFileProcess(plan.command, cancel_check=lambda: self._cancel_requested)
                            if result.cancelled or self._cancel_requested:
                                cancelled = True
                                done -= 1
                                break
                            if not result.succeeded:
                                failed += 1
                                self.logMessage.emit(LOG_ERROR, f"{source.name} 压缩失败：{result.stderr or 'ffmpeg 返回未知错误'}")
                            elif commit_output(plan):
                                ok += 1
                                self.logMessage.emit(LOG_OK, f"{source.name} → {destination.name} 完成")
                            else:
                                skipped += 1
                                self.logMessage.emit(LOG_WARN, f"已跳过（输出已存在）：{destination.name}")
                    except (OSError, ConverterError, ValueError) as exc:
                        failed += 1
                        self.logMessage.emit(LOG_ERROR, f"{source.name}：{exc}")
                    self._Progress(done, total, source.name)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            self.logMessage.emit(LOG_ERROR, f"线程内部异常：{exc}")
        if cancelled:
            self.logMessage.emit(LOG_WARN, "已取消，任务中止")
        elif early_stopped:
            self.logMessage.emit(LOG_WARN, "已按“结束”请求停止，未开始后续文件夹")
        else:
            self.logMessage.emit(LOG_INFO, f"任务结束：成功 {ok}，失败 {failed}，跳过 {skipped}")
        self.taskFinished.emit(TaskResult(
            total=total, ok=ok, failed=failed, skipped=skipped,
            cancelled=cancelled, early_stopped=early_stopped, output_dirs=output_dirs,
        ))
