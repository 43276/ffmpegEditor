"""元数据编辑后台线程：导入读取、确认写入、封面导出。"""
from __future__ import annotations

import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from app.Converter import RunFileProcess
from app.MetadataEdit import (
    BuildOutputPlan,
    ExtractCoverThumbnail,
    ExtractCoverToFile,
    FormatByKey,
    MetadataError,
    ReadAudioInfo,
    TrackEdit,
)
from ui.Worker import LOG_ERROR, LOG_INFO, LOG_OK, LOG_WARN


class MetadataReadWorker(QThread):
    """导入后台线程：逐文件读取元数据与封面缩略图，不阻塞界面。"""

    logMessage = pyqtSignal(int, int, str)
    progressChanged = pyqtSignal(int, int, int, str)
    rowLoaded = pyqtSignal(int, object)
    taskFinished = pyqtSignal(int, dict)

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

            edit = TrackEdit.FromAudioInfo(info)
            if info.error is None and info.has_cover:
                thumbnail = ExtractCoverThumbnail(self._ffmpeg_path, path)
                edit.thumbnail_bytes = thumbnail

            self.rowLoaded.emit(self._thread_id, edit)
            self.progressChanged.emit(self._thread_id, done, total, path.name)

        self._Log(
            LOG_INFO,
            f"导入结束：成功读取 {ok_count}，失败 {failed_count}"
            + ("（已取消）" if cancelled else ""),
        )
        self.taskFinished.emit(self._thread_id, {
            "total": total,
            "ok": ok_count,
            "failed": failed_count,
            "cancelled": cancelled,
        })

    def _Log(self, level: int, text: str) -> None:
        self.logMessage.emit(self._thread_id, level, text)


class MetadataWriteWorker(QThread):
    """确认写入后台线程：把内存编辑真正写到磁盘。

    信号与 AlbumWorker 保持一致：
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

        used_outputs: set[str] = set()

        with tempfile.TemporaryDirectory(prefix="meta_covers_") as cover_temp_dir:
            for edit in self._edits:
                if self._cancel_requested:
                    cancelled = True
                    break

                done += 1

                # 格式转换但未改封面时，保留原内嵌封面：先提取出来再作为新封面写入。
                # 用副本传参，避免把提取结果写回界面的内存态。
                plan_edit = edit
                fmt = FormatByKey(edit.target_format_key)
                converting = fmt is not None and fmt.key != "keep"
                if edit.cover_action is None and converting and edit.has_original_cover:
                    extracted, error = ExtractCoverToFile(
                        self._ffmpeg_path, edit.path, cover_temp_dir, edit.cover_codec
                    )
                    if extracted is not None:
                        plan_edit = replace(
                            edit, cover_action="set", cover_source=extracted
                        )
                    else:
                        self._Log(
                            LOG_WARN,
                            f"{edit.file_name}：格式转换时提取原封面失败"
                            f"（{error}），输出文件可能不含封面",
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
                    self.progressChanged.emit(
                        self._thread_id, done, total, edit.file_name
                    )
                    emit_statistics()
                    continue

                for warning in plan.warnings:
                    self._Log(LOG_WARN, f"{edit.file_name}：{warning}")

                if not plan.replaces_source and plan.output_path.exists() and not self._overwrite:
                    skipped_count += 1
                    message = f"已跳过（输出文件已存在）：{plan.output_path.name}"
                    skipped_logs.append(message)
                    self._Log(LOG_WARN, message)
                    self.progressChanged.emit(self._thread_id, done, total, edit.file_name)
                    emit_statistics()
                    continue

                plan.output_path.parent.mkdir(parents=True, exist_ok=True)
                if str(plan.output_path.parent) not in output_dirs:
                    output_dirs.append(str(plan.output_path.parent))

                try:
                    plan.temp_path.unlink(missing_ok=True)
                except OSError as exc:
                    failed_count += 1
                    message = f"{edit.file_name}：无法清理临时文件：{exc}"
                    error_logs.append(message)
                    self._Log(LOG_ERROR, message)
                    self.progressChanged.emit(self._thread_id, done, total, edit.file_name)
                    emit_statistics()
                    continue

                return_code, error_tail, was_cancelled = RunFileProcess(
                    plan.command, cancel_check=self._CancelCheck
                )
                if was_cancelled:
                    plan.temp_path.unlink(missing_ok=True)
                    cancelled = True
                    done -= 1
                    break

                if return_code != 0:
                    plan.temp_path.unlink(missing_ok=True)
                    failed_count += 1
                    detail = error_tail or "ffmpeg 返回未知错误"
                    message = f"{edit.file_name} 处理失败：{detail}"
                    error_logs.append(message)
                    self._Log(LOG_ERROR, message)
                else:
                    try:
                        if plan.replaces_source and edit.backup:
                            backup_path = edit.path.with_name(edit.path.name + ".bak")
                            shutil.copy2(edit.path, backup_path)
                            self._Log(LOG_INFO, f"{edit.file_name}：已备份到 {backup_path.name}")
                        plan.temp_path.replace(plan.output_path)
                        ok_count += 1
                        action = "转换并写入" if plan.converted else "已修改"
                        self._Log(
                            LOG_OK,
                            f"{edit.file_name} → {plan.output_path.name} {action}",
                        )
                    except OSError as exc:
                        plan.temp_path.unlink(missing_ok=True)
                        failed_count += 1
                        message = f"{edit.file_name}：无法保存输出文件：{exc}"
                        error_logs.append(message)
                        self._Log(LOG_ERROR, message)

                self.progressChanged.emit(self._thread_id, done, total, edit.file_name)
                emit_statistics()

        if cancelled:
            self._Log(LOG_WARN, "已取消，写入中止")
        else:
            self._Log(
                LOG_INFO,
                f"确认修改结束：成功 {ok_count}，失败 {failed_count}，跳过 {skipped_count}",
            )

        emit_statistics()
        self.taskFinished.emit(self._thread_id, {
            "total": total,
            "ok": ok_count,
            "failed": failed_count,
            "skipped": skipped_count,
            "cancelled": cancelled,
            "output_dirs": output_dirs,
            "error_logs": error_logs,
            "skipped_logs": skipped_logs,
        })


class CoverExportWorker(QThread):
    """提取全部封面后台线程：读取磁盘上的原始封面，未确认的内存修改不算。"""

    logMessage = pyqtSignal(int, int, str)
    progressChanged = pyqtSignal(int, int, int, str)
    taskFinished = pyqtSignal(int, dict)

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

    def run(self) -> None:
        total = len(self._items)
        done = 0
        ok_count = 0
        skipped_count = 0
        failed_count = 0
        cancelled = False
        output_dir = Path(self._output_dir)

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._Log(LOG_ERROR, f"无法创建输出目录：{exc}")
            self.taskFinished.emit(self._thread_id, {
                "total": total, "ok": 0, "skipped": 0, "failed": 0,
                "cancelled": False, "output_dir": str(output_dir),
            })
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
                _dest, error = ExtractCoverToFile(
                    self._ffmpeg_path, path, output_dir, cover_codec
                )
                if error is None:
                    ok_count += 1
                    self._Log(LOG_OK, f"{path.name}：封面已导出")
                else:
                    failed_count += 1
                    self._Log(LOG_ERROR, f"{path.name}：{error}")
            self.progressChanged.emit(self._thread_id, done, total, path.name)

        self._Log(
            LOG_INFO,
            f"封面导出结束：成功 {ok_count}，跳过 {skipped_count}，失败 {failed_count}"
            + ("（已取消）" if cancelled else ""),
        )
        self.taskFinished.emit(self._thread_id, {
            "total": total,
            "ok": ok_count,
            "skipped": skipped_count,
            "failed": failed_count,
            "cancelled": cancelled,
            "output_dir": str(output_dir),
        })
