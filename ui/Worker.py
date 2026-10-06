"""后台转换线程：负责一组待处理的批次（文件夹）
"""
from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal

from app.Converter import BuildCommand, ConverterError, OutputNameFor, RunFileProcess
from app.image.models import Batch, ConvertOptions
from app.ffmpeg_environment import FfmpegCapabilities

from app.output_files import (
    commit_output, ensure_output_directory, output_transaction,
    path_key, reserve_output_path, temporary_path_for,
)
from app.task_models import (
    LOG_ERROR, LOG_INFO, LOG_OK, LOG_WARN, FilePlan, TaskProgress, TaskResult,
)


class ConvertWorker(QThread):
    """单个后台转换线程。

    信号：
    - logMessage(int 线程号, int 级别, str 文本)：追加一条日志；
    - progressChanged(int 线程号, int 已完成, int 总数, str 当前文件)：进度变化；
    - taskFinished(int 线程号, TaskResult)：线程结束汇总
      包含计数、停止状态、输出目录与日志。
    """

    logMessage = pyqtSignal(int, int, str)
    progressChanged = pyqtSignal(int, int, int, str)
    statisticsChanged = pyqtSignal(int, int, int, int, int, int)
    taskFinished = pyqtSignal(int, object)

    def __init__(
        self,
        thread_id: int,
        batches: list[Batch],
        options: ConvertOptions,
        capabilities: FfmpegCapabilities,
        parent=None,
    ):
        super().__init__(parent)
        self._thread_id = thread_id
        self._batches = batches
        self._options = options
        self._capabilities = capabilities
        self._cancel_requested = False
        self._finish_requested = False

    def RequestCancel(self) -> None:
        """硬取消：尽快终止（含正在运行的 ffmpeg）。"""
        self._cancel_requested = True

    def RequestFinishAfterCurrentBatch(self) -> None:
        """软结束：处理完当前正在处理的批次（文件夹）后再停止，
        不开始任何新的批次。取消优先级高于此请求。"""
        self._finish_requested = True

    def _CancelCheck(self) -> bool:
        return self._cancel_requested

    def _Log(self, level: int, text: str) -> None:
        self.logMessage.emit(self._thread_id, level, text)

    def _Progress(self, done: int, total: int, current_file: str) -> None:
        progress = TaskProgress(done, total, current_file)
        self.progressChanged.emit(self._thread_id, progress.done, progress.total, progress.current_file)

    def run(self) -> None:
        options = self._options
        total_files = sum(len(batch.files) for batch in self._batches)
        done = 0
        ok_count = 0
        failed_count = 0
        skipped_count = 0
        cancelled = False
        early_stopped = False
        used_outputs = {path_key(source) for batch in self._batches for source in batch.files}
        output_dirs: list[str] = []
        error_logs: list[str] = []
        skipped_logs: list[str] = []

        def emit_statistics() -> None:
            self.statisticsChanged.emit(
                self._thread_id,
                total_files,
                ok_count,
                failed_count,
                skipped_count,
                done,
            )


        try:
            self._Log(
                LOG_INFO, f"任务开始：{len(self._batches)} 个批次、{total_files} 个文件"
            )

            for batch in self._batches:
                if self._cancel_requested:
                    cancelled = True
                    break
                # 软结束：上一个批次已处理完（或尚未开始），不再开始新批次
                if self._finish_requested:
                    early_stopped = True
                    break
                ensure_output_directory(batch.output_dir)
                if str(batch.output_dir) not in output_dirs:
                    output_dirs.append(str(batch.output_dir))
                self._Log(
                    LOG_INFO,
                    f"批次 {batch.folder.name or batch.folder}："
                    f"{len(batch.files)} 个文件 → {batch.output_dir}",
                )

                for src_file in batch.files:
                    if self._cancel_requested:
                        cancelled = True
                        break

                    dst_name = OutputNameFor(src_file, options.target_extension)
                    dst_path = reserve_output_path(batch.output_dir / dst_name, used_outputs)
                    dst_name = dst_path.name
                    done += 1

                    if dst_path.exists() and not options.overwrite:
                        skipped_count += 1
                        message = f"已跳过（输出文件已存在）：{dst_name}"
                        skipped_logs.append(message)
                        self._Log(LOG_WARN, message)
                        self._Progress(done, total_files, dst_name)
                        emit_statistics()
                        continue

                    temp_path = temporary_path_for(dst_path)
                    try:
                        cmd = BuildCommand(options, self._capabilities, src_file)
                        cmd.append(str(temp_path))
                        file_plan = FilePlan(src_file, dst_path, temp_path, tuple(cmd),
                                             overwrite=options.overwrite)
                        with output_transaction(file_plan):
                            result = RunFileProcess(file_plan.command, cancel_check=self._CancelCheck)
                            if result.cancelled or self._cancel_requested:
                                cancelled = True
                                done -= 1
                                break
                            if not result.succeeded:
                                failed_count += 1
                                message = f"{src_file.name} 转换失败：{result.stderr or 'ffmpeg 返回未知错误'}"
                                error_logs.append(message)
                                self._Log(LOG_ERROR, message)
                            elif commit_output(file_plan):
                                ok_count += 1
                                self._Log(LOG_OK, f"{src_file.name} → {dst_name} 完成")
                            else:
                                skipped_count += 1
                                message = f"已跳过（输出文件已存在）：{dst_name}"
                                skipped_logs.append(message)
                                self._Log(LOG_WARN, message)
                    except (OSError, ConverterError, ValueError) as exc:
                        failed_count += 1
                        message = f"{src_file.name}：{exc}"
                        error_logs.append(message)
                        self._Log(LOG_ERROR, message)
                    self._Progress(done, total_files, src_file.name)
                    emit_statistics()
        except Exception as exc:  # noqa: BLE001 —— 保证线程异常时也能汇报并结束
            failed_count += 1
            message = f"线程内部异常：{exc}"
            error_logs.append(message)
            self._Log(LOG_ERROR, message)

        if cancelled:
            self._Log(LOG_WARN, "已取消，任务中止")
        elif early_stopped:
            self._Log(
                LOG_WARN,
                "已按“结束”请求停止：完成当前批次后不再处理后续批次"
                f"（成功 {ok_count}，失败 {failed_count}，跳过 {skipped_count}）",
            )
        else:
            self._Log(
                LOG_INFO,
                f"任务结束：成功 {ok_count}，失败 {failed_count}，跳过 {skipped_count}",
            )

        emit_statistics()

        self.taskFinished.emit(self._thread_id, TaskResult(
            total=total_files,
            ok=ok_count,
            failed=failed_count,
            skipped=skipped_count,
            cancelled=cancelled,
            early_stopped=early_stopped,
            output_dirs=output_dirs,
            error_logs=error_logs,
            skipped_logs=skipped_logs,
        ))
