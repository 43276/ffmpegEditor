"""元数据编辑后台线程：导入读取、确认写入、封面导出。"""
from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from app.Converter import RunFileProcess
from app.MetadataEdit import (
    BuildOutputPlan,
    ExtractCoverThumbnail,
    ExtractCoverToFile,
    MetadataError,
    ReadAudioInfo,
)
from app.audio.models import TrackEdit
from app.audio.formats import format_by_key
from app.output_files import (
    commit_output, ensure_output_directory, output_transaction,
    path_key,
)
from app.task_models import (
    LOG_ERROR, LOG_INFO, LOG_OK, LOG_WARN, TaskProgress, TaskResult,
)


class MetadataReadWorker(QThread):
    """导入后台线程：逐文件读取元数据与封面缩略图，不阻塞界面。"""

    logMessage = pyqtSignal(int, int, str)
    progressChanged = pyqtSignal(int, int, int, str)
    rowLoaded = pyqtSignal(int, object)
    taskFinished = pyqtSignal(int, object)

    def __init__(
        self,
        thread_id: int,
        ffprobe_path: str,
        ffmpeg_path: str,
        files: list[Path],
        parent=None,
    ):
        super().__init__(parent)
        self._thread_id = thread_id
        self._ffprobe_path = ffprobe_path
        self._ffmpeg_path = ffmpeg_path
        self._files = files
        self._cancel_requested = False

    def RequestCancel(self) -> None:
        self._cancel_requested = True

    def _Progress(self, done: int, total: int, current_file: str) -> None:
        progress = TaskProgress(done, total, current_file)
        self.progressChanged.emit(self._thread_id, progress.done, progress.total, progress.current_file)

    def run(self) -> None:
        total = len(self._files)
        done = 0
        ok_count = 0
        failed_count = 0
        cancelled = False

        for path in self._files:
            if self._cancel_requested:
                cancelled = True
                break

            info = ReadAudioInfo(self._ffprobe_path, path)
            done += 1

            if info.error is None:
                ok_count += 1
            else:
                failed_count += 1
                self._Log(LOG_WARN, f"{path.name}：{info.error}")

            edit = TrackEdit.from_audio_info(info)
            if info.error is None and info.has_cover:
                thumbnail = ExtractCoverThumbnail(self._ffmpeg_path, path)
                edit.thumbnail_bytes = thumbnail

            self.rowLoaded.emit(self._thread_id, edit)
            self._Progress(done, total, path.name)

        self._Log(
            LOG_INFO,
            f"导入结束：成功读取 {ok_count}，失败 {failed_count}"
            + ("（已取消）" if cancelled else ""),
        )
        self.taskFinished.emit(self._thread_id, TaskResult(
            total=total,
            ok=ok_count,
            failed=failed_count,
            cancelled=cancelled,
        ))

    def _Log(self, level: int, text: str) -> None:
        self.logMessage.emit(self._thread_id, level, text)


class MetadataWriteWorker(QThread):
    """确认写入后台线程：把内存编辑真正写到磁盘。

    信号与 AlbumWorker 保持一致：
    - logMessage(int 线程号, int 级别, str 文本)
    - progressChanged(int 线程号, int 已完成, int 总数, str 当前文件)
    - statisticsChanged(int 线程号, int 总数, int 成功, int 失败, int 跳过, int 已完成)
    - taskFinished(int 线程号, TaskResult 汇总)
    """

    logMessage = pyqtSignal(int, int, str)
    progressChanged = pyqtSignal(int, int, int, str)
    statisticsChanged = pyqtSignal(int, int, int, int, int, int)
    taskFinished = pyqtSignal(int, object)

    def __init__(
        self,
        thread_id: int,
        ffmpeg_path: str,
        edits: list[TrackEdit],
        overwrite: bool,
        in_place: bool,
        output_root: str | None,
        parent=None,
    ):
        super().__init__(parent)
        self._thread_id = thread_id
        self._ffmpeg_path = ffmpeg_path
        self._edits = edits
        self._overwrite = overwrite
        self._in_place = in_place
        self._output_root = output_root
        self._cancel_requested = False

    def RequestCancel(self) -> None:
        self._cancel_requested = True

    def _CancelCheck(self) -> bool:
        return self._cancel_requested

    def _Log(self, level: int, text: str) -> None:
        self.logMessage.emit(self._thread_id, level, text)

    def _Progress(self, done: int, total: int, current_file: str) -> None:
        progress = TaskProgress(done, total, current_file)
        self.progressChanged.emit(self._thread_id, progress.done, progress.total, progress.current_file)

    def run(self) -> None:
        total = len(self._edits)
        done = 0
        ok_count = 0
        failed_count = 0
        skipped_count = 0
        cancelled = False
        output_dirs: list[str] = []
        error_logs: list[str] = []
        skipped_logs: list[str] = []

        def emit_statistics() -> None:
            self.statisticsChanged.emit(
                self._thread_id, total, ok_count, failed_count, skipped_count, done
            )

        self._Log(
            LOG_INFO,
            f"确认修改开始：{total} 个文件，"
            f"写入方式 {'原地修改' if self._in_place else '输出到新文件'}",
        )

        used_outputs = {path_key(edit.path) for edit in self._edits}

        with tempfile.TemporaryDirectory(prefix="meta_covers_") as cover_temp_dir:
            for edit in self._edits:
                if self._cancel_requested:
                    cancelled = True
                    break

                done += 1

                # 格式转换但未改封面时，保留原内嵌封面：先提取出来再作为新封面写入。
                # 用副本传参，避免把提取结果写回界面的内存态。
                plan_edit = edit
                fmt = format_by_key(edit.target_format_key)
                converting = fmt is not None and fmt.key != "keep"
                if edit.cover_action is None and converting and edit.has_original_cover:
                    extraction = ExtractCoverToFile(
                        self._ffmpeg_path, edit.path, cover_temp_dir, edit.cover_codec,
                        cancel_check=self._CancelCheck,
                    )
                    if extraction.cancelled:
                        cancelled = True
                        done -= 1
                        break
                    if extraction.path is not None:
                        plan_edit = replace(
                            edit, cover_action="set", cover_source=extraction.path
                        )
                    else:
                        self._Log(
                            LOG_WARN,
                            f"{edit.file_name}：格式转换时提取原封面失败"
                            f"（{extraction.error}），输出文件可能不含封面",
                        )

                try:
                    plan = BuildOutputPlan(
                        ffmpeg_path=self._ffmpeg_path,
                        edit=plan_edit,
                        overwrite=self._overwrite,
                        in_place=self._in_place,
                        output_root=self._output_root,
                        used_outputs=used_outputs,
                    )
                except MetadataError as exc:
                    failed_count += 1
                    message = f"{edit.file_name}：{exc}"
                    error_logs.append(message)
                    self._Log(LOG_ERROR, message)
                    self._Progress(done, total, edit.file_name)
                    emit_statistics()
                    continue

                for warning in plan.warnings:
                    self._Log(LOG_WARN, f"{edit.file_name}：{warning}")

                if not plan.replaces_source and plan.output_path.exists() and not plan.overwrite:
                    skipped_count += 1
                    message = f"已跳过（输出文件已存在）：{plan.output_path.name}"
                    skipped_logs.append(message)
                    self._Log(LOG_WARN, message)
                    self._Progress(done, total, edit.file_name)
                    emit_statistics()
                    continue

                try:
                    with output_transaction(plan):
                        if str(plan.output_path.parent) not in output_dirs:
                            output_dirs.append(str(plan.output_path.parent))
                        result = RunFileProcess(plan.command, cancel_check=self._CancelCheck)
                        if result.cancelled or self._cancel_requested:
                            cancelled = True
                            done -= 1
                            break
                        if not result.succeeded:
                            failed_count += 1
                            message = f"{edit.file_name} 处理失败：{result.stderr or 'ffmpeg 返回未知错误'}"
                            error_logs.append(message)
                            self._Log(LOG_ERROR, message)
                        elif commit_output(plan):
                            if plan.replaces_source and plan.backup:
                                self._Log(LOG_INFO, f"{edit.file_name}：已备份到 {edit.path.name}.bak")
                            ok_count += 1
                            action = "转换并写入" if plan.converted else "已修改"
                            self._Log(LOG_OK, f"{edit.file_name} → {plan.output_path.name} {action}")
                        else:
                            skipped_count += 1
                            message = f"已跳过（输出文件已存在）：{plan.output_path.name}"
                            skipped_logs.append(message)
                            self._Log(LOG_WARN, message)
                except Exception as exc:  # noqa: BLE001 -- 清理临时文件后汇报本文件失败
                    failed_count += 1
                    message = f"{edit.file_name}：无法保存输出文件：{exc}"
                    error_logs.append(message)
                    self._Log(LOG_ERROR, message)

                self._Progress(done, total, edit.file_name)
                emit_statistics()

        if cancelled:
            self._Log(LOG_WARN, "已取消，写入中止")
        else:
            self._Log(
                LOG_INFO,
                f"确认修改结束：成功 {ok_count}，失败 {failed_count}，跳过 {skipped_count}",
            )

        emit_statistics()
        self.taskFinished.emit(self._thread_id, TaskResult(
            total=total,
            ok=ok_count,
            failed=failed_count,
            skipped=skipped_count,
            cancelled=cancelled,
            output_dirs=output_dirs,
            error_logs=error_logs,
            skipped_logs=skipped_logs,
        ))


class CoverExportWorker(QThread):
    """提取全部封面后台线程：读取磁盘上的原始封面，未确认的内存修改不算。"""

    logMessage = pyqtSignal(int, int, str)
    progressChanged = pyqtSignal(int, int, int, str)
    taskFinished = pyqtSignal(int, object)

    def __init__(
        self,
        thread_id: int,
        ffmpeg_path: str,
        items: list[tuple[Path, str | None]],
        output_dir: str,
        parent=None,
    ):
        super().__init__(parent)
        self._thread_id = thread_id
        self._ffmpeg_path = ffmpeg_path
        self._items = items
        self._output_dir = output_dir
        self._cancel_requested = False

    def RequestCancel(self) -> None:
        self._cancel_requested = True

    def _Log(self, level: int, text: str) -> None:
        self.logMessage.emit(self._thread_id, level, text)

    def _Progress(self, done: int, total: int, current_file: str) -> None:
        progress = TaskProgress(done, total, current_file)
        self.progressChanged.emit(self._thread_id, progress.done, progress.total, progress.current_file)

    def run(self) -> None:
        total = len(self._items)
        done = 0
        ok_count = 0
        skipped_count = 0
        failed_count = 0
        cancelled = False
        output_dir = Path(self._output_dir)

        try:
            ensure_output_directory(output_dir)
        except OSError as exc:
            self._Log(LOG_ERROR, f"无法创建输出目录：{exc}")
            self.taskFinished.emit(self._thread_id, TaskResult(
                total=total, ok=0, skipped=0, failed=total,
                error_logs=[f"无法创建输出目录：{exc}"],
                cancelled=False, output_dirs=[str(output_dir)],
            ))
            return

        for path, cover_codec in self._items:
            if self._cancel_requested:
                cancelled = True
                break
            done += 1
            if cover_codec is None:
                skipped_count += 1
                self._Log(LOG_WARN, f"{path.name}：无内嵌封面，跳过")
            else:
                extraction = ExtractCoverToFile(
                    self._ffmpeg_path, path, output_dir, cover_codec,
                    cancel_check=lambda: self._cancel_requested,
                )
                if extraction.cancelled:
                    cancelled = True
                    done -= 1
                    break
                if extraction.error is None:
                    ok_count += 1
                    self._Log(LOG_OK, f"{path.name}：封面已导出")
                else:
                    failed_count += 1
                    self._Log(LOG_ERROR, f"{path.name}：{extraction.error}")
            self._Progress(done, total, path.name)

        self._Log(
            LOG_INFO,
            f"封面导出结束：成功 {ok_count}，跳过 {skipped_count}，失败 {failed_count}"
            + ("（已取消）" if cancelled else ""),
        )
        self.taskFinished.emit(self._thread_id, TaskResult(
            total=total,
            ok=ok_count,
            skipped=skipped_count,
            failed=failed_count,
            cancelled=cancelled,
            output_dirs=[str(output_dir)],
        ))
