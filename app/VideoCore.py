"""视频压缩的输入规则、批次规划与 FFmpeg 命令构建。"""
from __future__ import annotations

from .video.models import VideoBatch, VideoCompressOptions
from datetime import datetime
from pathlib import Path

from app.errors import FfmpegError as ConverterError
from app.ffmpeg_environment import FfmpegCapabilities

OUTPUT_SUFFIX = "_output"
SUPPORTED_VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", ".ts",
}


class VideoInputError(Exception):
    """输入不符合视频处理规则时抛出，消息可直接展示给用户。"""


def IsVideoFile(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS


def BuildVideoBatchesForInputs(
    input_paths: list[str | Path], include_output_dirs: bool = False,
) -> list[VideoBatch]:
    """按图片模块相同的 A/B/C 规则把视频输入解析为批次。"""
    paths = [Path(path) for path in input_paths]
    if not paths:
        raise VideoInputError("请先选择输入视频或文件夹")
    missing = next((path for path in paths if not path.exists()), None)
    if missing:
        raise VideoInputError(f"路径不存在：{missing}")

    all_files = all(path.is_file() for path in paths)
    all_dirs = all(path.is_dir() for path in paths)
    if not all_files and not all_dirs:
        raise VideoInputError("不能同时选择文件和文件夹")
    if all_files:
        invalid = next((path for path in paths if not IsVideoFile(path)), None)
        if invalid:
            raise VideoInputError(f"不支持的视频类型：{invalid.name}")
        parents = {path.resolve().parent for path in paths}
        if len(parents) != 1:
            raise VideoInputError("多选视频必须位于同一个文件夹内")
        parent = paths[0].parent
        if len(paths) == 1:
            output_dir = parent / f"{paths[0].stem}{OUTPUT_SUFFIX}"
        else:
            output_dir = parent / f"{MakeTaskOutputName()}{OUTPUT_SUFFIX}"
        return [VideoBatch(parent, output_dir, sorted(paths, key=lambda path: path.name.lower()))]

    if len(paths) > 1 and len({path.resolve().parent for path in paths}) != 1:
        raise VideoInputError("多选文件夹必须位于同一个上级文件夹内")
    batches: list[VideoBatch] = []
    for path in paths:
        _CollectVideoBatches(path, batches, include_output_dirs)
    if not batches:
        raise VideoInputError("该位置没有找到可处理的视频")
    return batches


def _CollectVideoBatches(folder: Path, batches: list[VideoBatch], include_output_dirs: bool) -> None:
    sub_dirs = [
        path for path in folder.iterdir()
        if path.is_dir() and (include_output_dirs or not path.name.endswith(OUTPUT_SUFFIX))
    ]
    videos = sorted(
        (path for path in folder.iterdir() if IsVideoFile(path)),
        key=lambda path: path.name.lower(),
    )
    if sub_dirs:
        for sub_dir in sub_dirs:
            _CollectVideoBatches(sub_dir, batches, include_output_dirs)
    elif videos:
        batches.append(VideoBatch(folder, folder / f"{folder.name}{OUTPUT_SUFFIX}", videos))


def MakeTaskOutputName(started_at: datetime | None = None) -> str:
    return (started_at or datetime.now()).strftime("%Y-%m-%d_%H-%M-%S")


def SummarizeVideoBatches(batches: list[VideoBatch]) -> str:
    total = sum(len(batch.files) for batch in batches)
    if len(batches) == 1:
        batch = batches[0]
        return f"将压缩 {total} 个视频，输出到：{batch.output_dir}"
    return f"发现 {len(batches)} 个待处理目录、共 {total} 个视频（首个输出目录：{batches[0].output_dir}）"


def OutputVideoName(source: Path) -> str:
    """首个版本统一输出兼容性较好的 MP4，保留源文件名。"""
    return f"{source.stem}.mp4"


def BuildVideoCompressCommand(
    options: VideoCompressOptions, capabilities: FfmpegCapabilities, source: Path,
) -> list[str]:
    if options.encoder not in capabilities.encoders:
        readable = "H.264 (libx264)" if options.encoder == "libx264" else "H.265 / HEVC (libx265)"
        raise ConverterError(f"当前 ffmpeg 缺少 {readable} 编码器")
    command = [
        options.ffmpeg_path, "-hide_banner", "-loglevel", "error",
        "-y" if options.overwrite else "-n", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0",
        "-c:v", options.encoder, "-crf", str(options.crf), "-preset", options.preset,
        "-pix_fmt", "yuv420p",
    ]
    if options.encoder == "libx265":
        command += ["-tag:v", "hvc1"]
    command += ["-c:a", "aac", "-b:a", options.audio_bitrate, "-movflags", "+faststart"]
    return command
