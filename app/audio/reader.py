"""音频输入收集、ffprobe 解析与封面读取；执行统一使用 Converter。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .cover_commands import build_cover_thumbnail_command
from .formats import AUDIO_EXTENSIONS, TEXT_FIELDS
from .metadata_planner import MetadataError
from .models import AudioInfo, TrackEdit
from app import converter
from app.errors import FfmpegError
from app.ffmpeg_environment import locate_ffprobe
from app.input_paths import walk_files, unique_input_paths, check_scan_cancelled
from app.task_models import LogEvent, LogLevel, TaskProgress, TaskResult


def is_audio_file(path: Path) -> bool:
    """元数据导入排除隐藏和临时文件；专辑扫描使用自己的过滤规则。"""
    return (path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS
            and not path.name.startswith(".") and ".part." not in path.name.lower()
            and not path.name.lower().endswith(".part"))


def collect_audio_files(folder: str | Path, *, cancel_check=None) -> list[Path]:
    check_scan_cancelled(cancel_check)
    root = Path(folder)
    if not root.exists():
        raise MetadataError(f"路径不存在：{root}")
    if not root.is_dir():
        raise MetadataError(f"不是文件夹：{root}")
    return sorted((path for path in walk_files(root, cancel_check=cancel_check) if is_audio_file(path)),
                  key=lambda path: str(path).lower())


def collect_audio_inputs(paths, *, cancel_check=None) -> list[Path]:
    """元数据允许混选文件和目录；收集、过滤与去重都在后台调用。"""
    collected = []
    for path in unique_input_paths(paths, cancel_check=cancel_check):
        check_scan_cancelled(cancel_check)
        if path.is_dir():
            collected.extend(collect_audio_files(path, cancel_check=cancel_check))
        elif is_audio_file(path):
            collected.append(path)
        elif not path.exists():
            raise MetadataError(f"路径不存在：{path}")
    return unique_input_paths(collected, cancel_check=cancel_check)


def locate_audio_ffprobe(ffmpeg_path: str | None = None) -> str:
    try:
        return locate_ffprobe(ffmpeg_path)
    except FfmpegError as exc:
        raise MetadataError(str(exc)) from exc


def read_audio_info(
    ffprobe_path: str, audio_path: Path, *, cancel_check: Callable[[], bool] | None = None,
) -> AudioInfo:
    command = [
        ffprobe_path, "-v", "error", "-show_entries",
        "format_tags:stream=codec_type,codec_name:stream_disposition=attached_pic",
        "-of", "json", str(audio_path),
    ]
    result = converter.execute(command, cancel_check=cancel_check, timeout_seconds=60, capture_stdout=True)
    if result.cancelled or (cancel_check is not None and cancel_check()):
        return AudioInfo(audio_path, {}, False, None, cancelled=True)
    if not result.succeeded:
        error = "ffprobe 读取超时" if result.timed_out else result.stderr or "ffprobe 读取失败"
        return AudioInfo(audio_path, {}, False, None, error)
    try:
        data = json.loads(result.stdout.decode("utf-8", errors="replace") or "{}")
        tags = (data.get("format") or {}).get("tags") or {}
        values = {item.key: str(tags[item.key]).strip() for item in TEXT_FIELDS if tags.get(item.key) is not None}
        has_cover, cover_codec = False, None
        image_codecs = {"mjpeg", "jpeg", "png", "bmp", "webp", "gif", "tiff"}
        for stream in data.get("streams") or []:
            if stream.get("codec_type") != "video":
                continue
            codec = (stream.get("codec_name") or "").lower()
            if (stream.get("disposition") or {}).get("attached_pic") == 1:
                has_cover, cover_codec = True, stream.get("codec_name")
                break
            if codec in image_codecs and not has_cover:
                has_cover, cover_codec = True, stream.get("codec_name")
        return AudioInfo(audio_path, values, has_cover, cover_codec)
    except (ValueError, TypeError, AttributeError) as exc:
        return AudioInfo(audio_path, {}, False, None, f"ffprobe 输出解析失败：{exc}")


def extract_cover_thumbnail(
    ffmpeg_path: str, audio_path: Path, *, cancel_check: Callable[[], bool] | None = None,
) -> bytes | None:
    result = converter.execute(build_cover_thumbnail_command(ffmpeg_path, audio_path),
                               cancel_check=cancel_check, timeout_seconds=60, capture_stdout=True)
    return result.stdout if result.succeeded and result.stdout else None


def read_audio_edits(
    ffprobe_path: str, ffmpeg_path: str, files: list[Path], *,
    cancel_check: Callable[[], bool] | None = None,
    on_row: Callable[[TrackEdit], None] | None = None,
    on_log: Callable[[LogEvent], None] | None = None,
    on_progress: Callable[[TaskProgress], None] | None = None,
) -> TaskResult:
    result = TaskResult(total=len(files))
    def cancelled():
        return cancel_check is not None and cancel_check()
    for path in files:
        if cancelled():
            result.cancelled = True
            break
        try:
            info = read_audio_info(ffprobe_path, path, cancel_check=cancel_check)
            if info.cancelled or cancelled():
                result.cancelled = True
                break
            edit = TrackEdit.from_audio_info(info)
            if info.error is None and info.has_cover:
                edit.thumbnail_bytes = extract_cover_thumbnail(ffmpeg_path, path, cancel_check=cancel_check)
            if cancelled():
                result.cancelled = True
                break
        except Exception as exc:
            info = AudioInfo(path, {}, False, None, str(exc))
            edit = TrackEdit.from_audio_info(info)
        if info.error is None:
            result.ok += 1
        else:
            result.failed += 1
            message = f"{path.name}：{info.error}"
            result.error_logs.append(message)
            if on_log is not None:
                on_log(LogEvent(LogLevel.WARN, message))
        if on_row is not None:
            on_row(edit)
        if on_progress is not None:
            on_progress(TaskProgress(result.ok + result.failed, result.total, path.name))
    if on_log is not None:
        on_log(LogEvent(LogLevel.INFO, f"导入结束：成功读取 {result.ok}，失败 {result.failed}"
                        + ("（已取消）" if result.cancelled else "")))
    return result
