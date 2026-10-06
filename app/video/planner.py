"""视频输入解析与 MP4 输出规划。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .models import VideoBatch, VideoCompressOptions
from .commands import build_video_compress_command
from app.ffmpeg_environment import FfmpegCapabilities
from app.input_paths import collect_leaf_media, validate_inputs, check_scan_cancelled
from app.output_files import path_key, reserve_output_path, temporary_path_for
from app.task_models import BatchPlan, FilePlan, TaskPlan
from app.errors import FfmpegError

OUTPUT_SUFFIX = "_output"
SUPPORTED_VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", ".ts",
}


class VideoInputError(Exception):
    """输入不符合视频处理规则时抛出，消息可直接展示给用户。"""


def is_video_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS


def build_video_batches_for_inputs(
    input_paths: list[str | Path], include_output_dirs: bool = False,
    *, cancel_check=None,
) -> list[VideoBatch]:
    """按图片模块相同的 A/B/C 规则把视频输入解析为批次。"""
    paths = validate_inputs(input_paths, VideoInputError,
                            empty_message="请先选择输入视频或文件夹",
                            same_parent_message="多选视频必须位于同一个文件夹内",
                            cancel_check=cancel_check)
    all_files = all(path.is_file() for path in paths)
    if all_files:
        invalid = next((path for path in paths if not is_video_file(path)), None)
        if invalid:
            raise VideoInputError(f"不支持的视频类型：{invalid.name}")
        parent = paths[0].parent
        if len(paths) == 1:
            output_dir = parent / f"{paths[0].stem}{OUTPUT_SUFFIX}"
        else:
            output_dir = parent / f"{make_task_output_name()}{OUTPUT_SUFFIX}"
        return [VideoBatch(parent, output_dir, sorted(paths, key=lambda path: path.name.lower()))]

    batches: list[VideoBatch] = []
    for path in paths:
        _collect_video_batches(path, batches, include_output_dirs, cancel_check=cancel_check)
    if not batches:
        raise VideoInputError("该位置没有找到可处理的视频")
    return batches


def _collect_video_batches(folder: Path, batches: list[VideoBatch], include_output_dirs: bool,
                           *, cancel_check=None) -> None:
    for leaf, videos in collect_leaf_media(
        folder, is_video_file, include_output_dirs=include_output_dirs, cancel_check=cancel_check,
    ):
        batches.append(VideoBatch(leaf, leaf / f"{leaf.name}{OUTPUT_SUFFIX}", videos))


def make_task_output_name(started_at: datetime | None = None) -> str:
    return (started_at or datetime.now()).strftime("%Y-%m-%d_%H-%M-%S")


def output_video_name(source: Path) -> str:
    """首个版本统一输出兼容性较好的 MP4，保留源文件名。"""
    return f"{source.stem}.mp4"


def build_video_task(
    batches: list[VideoBatch], options: VideoCompressOptions, capabilities: FfmpegCapabilities,
    *, cancel_check=None,
) -> TaskPlan:
    taken = {path_key(source) for batch in batches for source in batch.files}
    planned_batches: list[BatchPlan] = []
    for batch in batches:
        files: list[FilePlan] = []
        for source in batch.files:
            check_scan_cancelled(cancel_check)
            destination = reserve_output_path(batch.output_dir / output_video_name(source), taken)
            temporary = temporary_path_for(destination)
            command: tuple[str, ...] = ()
            error = None
            try:
                command = tuple(build_video_compress_command(options, capabilities, source, temporary))
            except (FfmpegError, ValueError) as exc:
                error = str(exc)
            files.append(FilePlan(source, destination, temporary, command,
                                  overwrite=options.overwrite, error=error))
        planned_batches.append(BatchPlan(batch.folder.name, tuple(files)))
    return TaskPlan(tuple(planned_batches))
