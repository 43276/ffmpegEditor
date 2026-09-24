"""后台转换线程：负责一组待处理的批次（文件夹）
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from app.Converter import BuildCommand, ConverterError, OutputNameFor, RunFileProcess
from app.Core import Batch, ConvertOptions

LOG_INFO = 0
LOG_OK = 1
LOG_WARN = 2
LOG_ERROR = 3


class ConvertWorker(QThread):
    """单个后台转换线程。

    信号：
    - logMessage(int 线程号, int 级别, str 文本)：追加一条日志；
    - progressChanged(int 线程号, int 已完成, int 总数, str 当前文件)：进度变化；
    - taskFinished(int 线程号, dict)：线程结束汇总
      {total, ok, failed, skipped, cancelled, output_dirs}。
    """

    logMessage = pyqtSignal(int, int, str)
    progressChanged = pyqtSignal(int, int, int, str)
    statisticsChanged = pyqtSignal(int, int, int, int, int, int)
    taskFinished = pyqtSignal(int, dict)

    def __init__(
        self,
        thread_id: int,
        batches: list[Batch],
        options: ConvertOptions,
        capabilities,
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

    def run(self) -> None:
        options = self._options
        total_files = sum(len(batch.files) for batch in self._batches)
        done = 0
        ok_count = 0
        failed_count = 0
        skipped_count = 0
        cancelled = False
        early_stopped = False
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
                batch.output_dir.mkdir(parents=True, exist_ok=True)
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
                    dst_path = batch.output_dir / dst_name
                    done += 1

                    if dst_path.exists() and not options.overwrite:
                        skipped_count += 1
                        message = f"已跳过（输出文件已存在）：{dst_name}"
                        skipped_logs.append(message)
                        self._Log(LOG_WARN, message)
                        self.progressChanged.emit(self._thread_id, done, total_files, dst_name)
                        emit_statistics()
                        continue

                    temp_path = dst_path.with_name(f".{dst_path.name}.part{dst_path.suffix}")
                    try:
                        temp_path.unlink(missing_ok=True)
                    except OSError as exc:
                        failed_count += 1
                        message = f"{src_file.name}：无法清理临时文件：{exc}"
                        error_logs.append(message)
                        self._Log(LOG_ERROR, message)
                        self.progressChanged.emit(self._thread_id, done, total_files, src_file.name)
                        emit_statistics()
                        continue

                    try:
                        cmd = BuildCommand(options, self._capabilities, src_file)
                        cmd.append(str(temp_path))  # 先写临时文件，成功后再替换正式文件
                    except ConverterError as exc:
                        failed_count += 1
                        message = f"{src_file.name}：{exc}"
                        error_logs.append(message)
                        self._Log(LOG_ERROR, message)
                        self.progressChanged.emit(self._thread_id, done, total_files, src_file.name)
                        emit_statistics()
                        continue

                    return_code, error_tail, was_cancelled = RunFileProcess(
                        cmd, cancel_check=self._CancelCheck
                    )
                    if was_cancelled:
                        temp_path.unlink(missing_ok=True)
                        cancelled = True
                        done -= 1
                        break
                    if return_code == 0:
                        try:
                            if not options.overwrite and dst_path.exists():
                                temp_path.unlink(missing_ok=True)
                                skipped_count += 1
                                message = f"已跳过（输出文件已存在）：{dst_name}"
                                skipped_logs.append(message)
                                self._Log(LOG_WARN, message)
                            else:
                                temp_path.replace(dst_path)
                                ok_count += 1
                                self._Log(LOG_OK, f"{src_file.name} → {dst_name} 完成")
                        except OSError as exc:
                            temp_path.unlink(missing_ok=True)
                            failed_count += 1
                            message = f"{src_file.name}：无法保存输出文件：{exc}"
                            error_logs.append(message)
                            self._Log(LOG_ERROR, message)
                    else:
                        temp_path.unlink(missing_ok=True)
                        failed_count += 1
                        detail = error_tail or "ffmpeg 返回未知错误"
                        message = f"{src_file.name} 转换失败：{detail}"
                        error_logs.append(message)
                        self._Log(LOG_ERROR, message)
                    self.progressChanged.emit(self._thread_id, done, total_files, src_file.name)
                    emit_statistics()
        except Exception as exc:  # noqa: BLE001 —— 保证线程异常时也能汇报并结束
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

        self.taskFinished.emit(self._thread_id, {
            "total": total_files,
            "ok": ok_count,
            "failed": failed_count,
            "skipped": skipped_count,
            "cancelled": cancelled,
            "early_stopped": early_stopped,
            "output_dirs": output_dirs,
            "error_logs": error_logs,
            "skipped_logs": skipped_logs,
        })
