"""视频压缩后台线程。"""
from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal

from app.Converter import ConverterError, RunFileProcess
from app.VideoCore import BuildVideoCompressCommand, OutputVideoName, VideoBatch, VideoCompressOptions
from ui.Worker import LOG_ERROR, LOG_INFO, LOG_OK, LOG_WARN


class VideoCompressWorker(QThread):
    logMessage = pyqtSignal(int, str)
    progressChanged = pyqtSignal(int, int, str)
    taskFinished = pyqtSignal(dict)

    def __init__(self, batches: list[VideoBatch], options: VideoCompressOptions, capabilities, parent=None):
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

    def run(self) -> None:
        total = sum(len(batch.files) for batch in self._batches)
        done = ok = failed = skipped = 0
        cancelled = early_stopped = False
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
                batch.output_dir.mkdir(parents=True, exist_ok=True)
                output_dirs.append(str(batch.output_dir))
                for source in batch.files:
                    if self._cancel_requested:
                        cancelled = True
                        break
                    destination = batch.output_dir / OutputVideoName(source)
                    if destination.exists() and not self._options.overwrite:
                        done += 1
                        skipped += 1
                        self.logMessage.emit(LOG_WARN, f"已跳过（输出已存在）：{destination.name}")
                        self.progressChanged.emit(done, total, source.name)
                        continue
                    temporary = destination.with_name(f".{destination.name}.part{destination.suffix}")
                    try:
                        temporary.unlink(missing_ok=True)
                        command = BuildVideoCompressCommand(self._options, self._capabilities, source)
                        command.append(str(temporary))
                    except (OSError, ConverterError) as exc:
                        done += 1
                        failed += 1
                        self.logMessage.emit(LOG_ERROR, f"{source.name}：{exc}")
                        self.progressChanged.emit(done, total, source.name)
                        continue
                    code, detail, was_cancelled = RunFileProcess(command, cancel_check=lambda: self._cancel_requested)
                    if was_cancelled:
                        temporary.unlink(missing_ok=True)
                        cancelled = True
                        break
                    done += 1
                    if code == 0:
                        try:
                            temporary.replace(destination)
                            ok += 1
                            self.logMessage.emit(LOG_OK, f"{source.name} → {destination.name} 完成")
                        except OSError as exc:
                            temporary.unlink(missing_ok=True)
                            failed += 1
                            self.logMessage.emit(LOG_ERROR, f"{source.name}：无法保存输出文件：{exc}")
                    else:
                        temporary.unlink(missing_ok=True)
                        failed += 1
                        self.logMessage.emit(LOG_ERROR, f"{source.name} 压缩失败：{detail or 'ffmpeg 返回未知错误'}")
                    self.progressChanged.emit(done, total, source.name)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            self.logMessage.emit(LOG_ERROR, f"线程内部异常：{exc}")
        if cancelled:
            self.logMessage.emit(LOG_WARN, "已取消，任务中止")
        elif early_stopped:
            self.logMessage.emit(LOG_WARN, "已按“结束”请求停止，未开始后续文件夹")
        else:
            self.logMessage.emit(LOG_INFO, f"任务结束：成功 {ok}，失败 {failed}，跳过 {skipped}")
        self.taskFinished.emit({
            "total": total, "ok": ok, "failed": failed, "skipped": skipped,
            "cancelled": cancelled, "early_stopped": early_stopped, "output_dirs": output_dirs,
        })
