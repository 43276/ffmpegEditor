"""专辑批处理后台线程"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from app.AddCover import AlbumPlan, BuildFfmpegCommand, OutputPathFor
from app.Converter import RunFileProcess
from ui.Worker import LOG_ERROR, LOG_INFO, LOG_OK, LOG_WARN


class AlbumWorker(QThread):
    """单个专辑批处理后台线程。

    信号与 ConvertWorker 保持一致：
    - logMessage(int 线程号, int 级别, str 文本)
    - progressChanged(int 线程号, int 已完成, int 总数, str 当前文件)
    - statisticsChanged(int 线程号, int 总数, int 成功, int 失败, int 跳过, int 已完成)
    - taskFinished(int 线程号, dict 汇总)
    """

    logMessage = pyqtSignal(int, int, str)
    progressChanged = pyqtSignal(int, int, int, str)
    statisticsChanged = pyqtSignal(int, int, int, int, int, int)
    taskFinished = pyqtSignal(int, dict)

    def __init__(
        self,
        thread_id: int,
        plan: AlbumPlan,
        overwrite: bool,
        ffmpeg_path: str,
        parent=None,
    ):
        super().__init__(parent)
        self._thread_id = thread_id
        self._plan = plan
        self._overwrite = overwrite
        self._ffmpeg_path = ffmpeg_path
        self._cancel_requested = False
        self._finish_requested = False

    def RequestCancel(self) -> None:
        """硬取消：尽快终止（含正在运行的 ffmpeg）。"""
        self._cancel_requested = True

    def RequestFinishAfterCurrentBatch(self) -> None:
        """软结束：处理完当前正在处理的专辑后再停止。"""
        self._finish_requested = True

    def _CancelCheck(self) -> bool:
        return self._cancel_requested

    def _Log(self, level: int, text: str) -> None:
        self.logMessage.emit(self._thread_id, level, text)

    def run(self) -> None:
        plan = self._plan
        overwrite = self._overwrite
        total_files = plan.file_count
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
            for skipped_dir in plan.skipped_dirs:
                self._Log(
                    LOG_WARN,
                    f"[跳过文件夹] {skipped_dir.processing_dir.name}：{skipped_dir.reason}",
                )

            self._Log(
                LOG_INFO,
                f"任务开始：{len(plan.tasks)} 张专辑、{total_files} 个音频",
            )

            for task in plan.tasks:
                if self._cancel_requested:
                    cancelled = True
                    break
                if self._finish_requested:
                    early_stopped = True
                    break

                self._Log(
                    LOG_INFO,
                    f"专辑 {task.processing_dir.name}：{task.file_count} 个音频，"
                    f"封面 {task.image_path.name}",
                )

                for group in task.groups:
                    if self._cancel_requested:
                        cancelled = True
                        break
                    group.output_dir.mkdir(parents=True, exist_ok=True)
                    if str(group.output_dir) not in output_dirs:
                        output_dirs.append(str(group.output_dir))
                    self._Log(LOG_INFO, f"  输出目录：{group.output_dir}")

                    for audio_path in group.files:
                        if self._cancel_requested:
                            cancelled = True
                            break

                        output_path = OutputPathFor(
                            group.audio_dir, group.output_dir, audio_path
                        )
                        done += 1

                        if output_path.exists() and not overwrite:
                            skipped_count += 1
                            message = f"已跳过（输出文件已存在）：{output_path.name}"
                            skipped_logs.append(message)
                            self._Log(LOG_WARN, message)
                            self.progressChanged.emit(
                                self._thread_id, done, total_files, output_path.name
                            )
                            emit_statistics()
                            continue

                        temp_path = output_path.with_name(
                            f".{output_path.name}.part{output_path.suffix}"
                        )
                        try:
                            temp_path.unlink(missing_ok=True)
                        except OSError as exc:
                            failed_count += 1
                            message = f"{audio_path.name}：无法清理临时文件：{exc}"
                            error_logs.append(message)
                            self._Log(LOG_ERROR, message)
                            self.progressChanged.emit(
                                self._thread_id, done, total_files, audio_path.name
                            )
                            emit_statistics()
                            continue

                        try:
                            cmd = BuildFfmpegCommand(
                                ffmpeg=self._ffmpeg_path,
                                audio_path=audio_path,
                                image_path=task.image_path,
                                output_path=temp_path,
                                overwrite=overwrite,
                                metadata=task.metadata,
                            )
                        except Exception as exc:  # noqa: BLE001 —— 与图片模块保持一致
                            failed_count += 1
                            message = f"{audio_path.name}：{exc}"
                            error_logs.append(message)
                            self._Log(LOG_ERROR, message)
                            self.progressChanged.emit(
                                self._thread_id, done, total_files, audio_path.name
                            )
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
                                if not overwrite and output_path.exists():
                                    temp_path.unlink(missing_ok=True)
                                    skipped_count += 1
                                    message = f"已跳过（输出文件已存在）：{output_path.name}"
                                    skipped_logs.append(message)
                                    self._Log(LOG_WARN, message)
                                else:
                                    temp_path.replace(output_path)
                                    ok_count += 1
                                    self._Log(LOG_OK, f"{audio_path.name} → {output_path.name} 完成")
                            except OSError as exc:
                                temp_path.unlink(missing_ok=True)
                                failed_count += 1
                                message = f"{audio_path.name}：无法保存输出文件：{exc}"
                                error_logs.append(message)
                                self._Log(LOG_ERROR, message)
                        else:
                            temp_path.unlink(missing_ok=True)
                            failed_count += 1
                            detail = error_tail or "ffmpeg 返回未知错误"
                            message = f"{audio_path.name} 处理失败：{detail}"
                            error_logs.append(message)
                            self._Log(LOG_ERROR, message)

                        self.progressChanged.emit(
                            self._thread_id, done, total_files, audio_path.name
                        )
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
                "已按“结束”请求停止：完成当前专辑后不再处理后续专辑"
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
            "skipped_dirs": len(plan.skipped_dirs),
            "cancelled": cancelled,
            "early_stopped": early_stopped,
            "output_dirs": output_dirs,
            "error_logs": error_logs,
            "skipped_logs": skipped_logs,
        })
