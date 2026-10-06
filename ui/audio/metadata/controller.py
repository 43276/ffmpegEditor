"""管理元数据草稿、稳定行标识、读写任务与临时封面资源。"""
from __future__ import annotations

import os
import tempfile
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from PyQt6.QtCore import QObject, pyqtSignal

from app.audio.formats import METADATA_FIELDS
from app.audio.metadata_planner import apply_cover_edit
from app.audio.models import TrackEdit
from app.task_models import LOG_INFO, LogEvent, TaskResult, TaskStatistics
from ui.tasks.controller import TaskController
from ui.tasks.jobs import metadata_read_job, metadata_write_job


class CoverResources:
    """页面拥有粘贴图片；任务与弹窗持有租约，关闭后延迟清理。"""

    def __init__(self):
        self._directory = tempfile.TemporaryDirectory(prefix="meta_covers_ui_")
        self.path = Path(self._directory.name)
        self._leases: set[str] = set()
        self._cleanup_requested = False
        self._cleaned = False

    def acquire(self) -> str:
        if self._cleaned:
            raise RuntimeError("临时封面资源已经释放")
        token = uuid4().hex
        self._leases.add(token)
        return token

    def release(self, token: str) -> None:
        self._leases.discard(token)
        self._try_cleanup()

    def store_png(self, data: bytes) -> Path:
        if self._cleanup_requested:
            raise OSError("页面正在关闭，无法保存剪贴板图片")
        target = self.path / f"pasted_{uuid4().hex}.png"
        target.write_bytes(data)
        return target

    def request_cleanup(self) -> None:
        self._cleanup_requested = True
        self._try_cleanup()

    def _try_cleanup(self) -> None:
        if self._cleanup_requested and not self._leases and not self._cleaned:
            self._directory.cleanup()
            self._cleaned = True


class MetadataController(QObject):
    """编辑事件只接受行 ID；写入只接受隔离后的草稿快照。"""

    rowsReset = pyqtSignal()
    rowAdded = pyqtSignal(str, object)
    rowChanged = pyqtSignal(str, object)
    modifiedChanged = pyqtSignal(int)
    actionsChanged = pyqtSignal()
    logMessage = pyqtSignal(object)
    progressChanged = pyqtSignal(object)
    statisticsChanged = pyqtSignal(object)
    started = pyqtSignal(str, int)
    completed = pyqtSignal(str, object)
    notice = pyqtSignal(str, str, str)
    idle = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tasks = TaskController(self)
        self.resources = CoverResources()
        self.checked_fields = {field.key for field in METADATA_FIELDS if field.default_checked}
        self.ffmpeg_path: str | None = None
        self.ffprobe_path: str | None = None
        self.environment = None
        self.closing = False
        self.rows: list[TrackEdit] = []
        self._row_ids: list[str] = []
        self._edits: dict[str, TrackEdit] = {}
        self._identity_ids: dict[int, str] = {}
        self._loading = False
        self._path_ids: dict[str, str] = {}
        self._resource_token: str | None = None
        self._write_environment: tuple[str, str | None] | None = None

        self.tasks.logMessage.connect(self.logMessage)
        self.tasks.progressChanged.connect(self.progressChanged)
        self.tasks.statisticsChanged.connect(self.statisticsChanged)
        self.tasks.rowLoaded.connect(self.add_row)
        self.tasks.completed.connect(self._on_completed)
        self.tasks.stateChanged.connect(lambda _state: self.actionsChanged.emit())
        self.tasks.idle.connect(self._on_idle)

    @staticmethod
    def _path_key(path: Path) -> str:
        try:
            return os.path.normcase(str(path.resolve()))
        except OSError:
            return os.path.normcase(str(path.absolute()))

    @property
    def entries(self) -> list[tuple[str, TrackEdit]]:
        return [(row_id, self._edits[row_id]) for row_id in self._row_ids]

    def row_id(self, edit: TrackEdit) -> str | None:
        return self._identity_ids.get(id(edit))

    def row(self, row_id: str) -> TrackEdit | None:
        return self._edits.get(row_id)

    def replace_rows(self, edits) -> None:
        self.rows = []
        self._row_ids.clear()
        self._edits.clear()
        self._identity_ids.clear()
        self.rowsReset.emit()
        self._loading = True
        try:
            for edit in edits:
                self.add_row(edit)
        finally:
            self._loading = False
        self._changed()

    def add_row(self, edit: TrackEdit) -> None:
        if self.closing:
            return
        key = self._path_key(edit.path)
        row_id = self._path_ids.setdefault(key, uuid4().hex)
        if row_id in self._edits:
            index = self._row_ids.index(row_id)
            self._identity_ids.pop(id(self._edits[row_id]), None)
            self._identity_ids[id(edit)] = row_id
            self.rows[index] = edit
            self._edits[row_id] = edit
            self.rowChanged.emit(row_id, edit)
        else:
            self._row_ids.append(row_id)
            self._identity_ids[id(edit)] = row_id
            self._edits[row_id] = edit
            self.rows.append(edit)
            self.rowAdded.emit(row_id, edit)
        # 导入中的新行没有编辑，按钮已由任务状态禁用；结束后统一通知。
        if not self._loading and not self.tasks.active:
            self._changed()

    def set_environment(self, snapshot) -> None:
        self.environment = snapshot
        ready = snapshot is not None and snapshot.status == "ready"
        self.ffmpeg_path = snapshot.ffmpeg_path if ready else None
        self.ffprobe_path = snapshot.ffprobe_path if ready else None
        self.actionsChanged.emit()

    def _scope(self, row_id: str | None) -> list[TrackEdit]:
        rows = self.rows if row_id is None else [self._edits.get(row_id)]
        return [row for row in rows if row is not None and not row.error]

    def _editable(self) -> bool:
        return not self.closing and not self.tasks.active

    @property
    def modified_count(self) -> int:
        return sum(1 for row in self.rows if not row.error and (
            any(key in self.checked_fields for key in row.edited_values)
            or ("cover" in self.checked_fields and row.cover_action is not None)
            or row.target_format_key is not None))

    def _changed(self) -> None:
        self.modifiedChanged.emit(self.modified_count)
        self.actionsChanged.emit()

    def set_field_visible(self, key: str, checked: bool) -> None:
        if not self._editable():
            return
        if checked:
            self.checked_fields.add(key)
        else:
            self.checked_fields.discard(key)
        # 取消勾选只隐藏并屏蔽写入；再次勾选仍显示原草稿。
        self._changed()

    def edit_text(self, row_id: str, key: str, value: str) -> None:
        if not self._editable() or key not in self.checked_fields:
            return
        for row in self._scope(row_id):
            if value == row.original_values.get(key, ""):
                row.edited_values.pop(key, None)
            else:
                row.edited_values[key] = value
        self._changed()

    def apply_text(self, key: str, value: str, row_id: str | None = None) -> None:
        if not self._editable() or key not in self.checked_fields:
            return
        for row in self._scope(row_id):
            if value == row.original_values.get(key, ""):
                row.edited_values.pop(key, None)
            else:
                row.edited_values[key] = value
            self.rowChanged.emit(self.row_id(row), row)
        self._changed()

    def apply_format(self, key: str, bitrate: str | None, row_id: str | None = None) -> None:
        if not self._editable():
            return
        for row in self._scope(row_id):
            row.target_format_key = None if key == "keep" else key
            row.bitrate = None if key == "keep" else bitrate
            self.rowChanged.emit(self.row_id(row), row)
        self._changed()

    def apply_cover(self, action: str, source: Path | None, row_id: str | None = None,
                    *, auto_convert_wav: bool = False, wav_target_key: str = "mp3") -> None:
        if not self._editable() or "cover" not in self.checked_fields:
            return
        auto_converted = False
        for row in self._scope(row_id):
            updated = apply_cover_edit(row, action, source, auto_convert_wav=auto_convert_wav,
                                       wav_target_key=wav_target_key)
            auto_converted |= row.target_format_key != updated.target_format_key
            row.cover_action, row.cover_source = updated.cover_action, updated.cover_source
            row.target_format_key, row.bitrate = updated.target_format_key, updated.bitrate
            self.rowChanged.emit(self.row_id(row), row)
        if auto_converted:
            self.logMessage.emit(LogEvent(LOG_INFO, "wav 加封面：已自动设置转换格式（写入时转换）"))
        self._changed()

    def reset(self) -> None:
        if not self._editable():
            return
        for row in self.rows:
            row.edited_values.clear()
            row.cover_action = row.cover_source = row.target_format_key = row.bitrate = None
        self.rowsReset.emit()
        self._changed()

    def write_snapshot(self, *, backup: bool = False) -> list[TrackEdit]:
        edits = []
        for row in self.rows:
            if row.error:
                continue
            edit = deepcopy(row)
            edit.edited_values = {key: value for key, value in edit.edited_values.items()
                                  if key in self.checked_fields}
            if "cover" not in self.checked_fields:
                edit.cover_action = edit.cover_source = None
            edit.backup = backup
            if edit.modified:
                edits.append(edit)
        return edits

    def _begin(self, operation, *, total: int, kind: str) -> bool:
        if self._resource_token is None:
            self._resource_token = self.resources.acquire()
        started = self.tasks.start(operation, total=total, kind=kind)
        if started:
            self.started.emit(kind, total)
        if not started and not self.tasks.active:
            self._on_idle()
        return started

    def start_import(self, paths, *, environment: tuple[str, str | None] | None = None) -> bool:
        if not self._editable():
            self.notice.emit("请稍候", "正在读取或写入中", "warning")
            return False
        paths = tuple(Path(path) for path in paths)
        if not paths:
            return False
        ffmpeg, ffprobe = environment or (self.ffmpeg_path, self.ffprobe_path)
        if not ffmpeg or not ffprobe:
            self.notice.emit("无法开始", "FFmpeg / ffprobe 尚未就绪，请在设置页检查路径", "error")
            return False
        self.replace_rows([])
        self.statisticsChanged.emit(TaskStatistics(0, 0, 0, 0, 0))
        self.logMessage.emit(LogEvent(LOG_INFO, "正在收集并导入音频文件…"))
        # 文件夹遍历、混选过滤和去重均由后台读取 Job 完成。
        return self._begin(metadata_read_job(ffprobe, ffmpeg, paths), total=0, kind="read")

    def confirm_write(self, *, overwrite: bool, in_place: bool,
                      output_root: str | None, backup: bool) -> bool:
        if not self._editable():
            return False
        edits = self.write_snapshot(backup=backup if in_place else False)
        if not edits:
            self.notice.emit("没有修改", "表格中没有未确认的修改", "warning")
            return False
        if not self.ffmpeg_path:
            self.notice.emit("无法开始", "FFmpeg 尚未就绪，请在设置页检查路径", "error")
            return False
        if not in_place and not output_root:
            self.notice.emit("无法开始", "另存为需要先选择输出目录", "error")
            return False
        self._write_environment = (self.ffmpeg_path, self.ffprobe_path)
        self.statisticsChanged.emit(TaskStatistics(len(edits), 0, 0, 0, 0))
        self.logMessage.emit(LogEvent(LOG_INFO, f"确认修改开始：{len(edits)} 个文件"))
        return self._begin(metadata_write_job(self.ffmpeg_path, edits, overwrite, in_place, output_root),
                           total=len(edits), kind="write")

    def _on_completed(self, kind: str, summary: TaskResult) -> None:
        if kind == "read":
            self._changed()
        self.completed.emit(kind, summary)
        if kind == "write" and not self.closing and self.rows:
            self.start_import([row.path for row in self.rows], environment=self._write_environment)

    def _on_idle(self) -> None:
        if self._resource_token is not None:
            self.resources.release(self._resource_token)
            self._resource_token = None
        if self.closing:
            self.resources.request_cleanup()
        self.idle.emit()

    def shutdown(self) -> None:
        self.closing = True
        self.tasks.shutdown()
        self.actionsChanged.emit()
