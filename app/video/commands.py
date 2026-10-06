"""构建完整视频压缩命令，不执行进程。"""
from __future__ import annotations

from pathlib import Path

from .models import VideoCompressOptions
from app.errors import FfmpegError
from app.ffmpeg_environment import FfmpegCapabilities


def build_video_compress_command(
    options: VideoCompressOptions, capabilities: FfmpegCapabilities, source: Path, output_path: Path,
) -> list[str]:
    if options.encoder not in capabilities.encoders:
        readable = "H.264 (libx264)" if options.encoder == "libx264" else "H.265 / HEVC (libx265)"
        raise FfmpegError(f"当前 ffmpeg 缺少 {readable} 编码器")
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
    return [*command, str(output_path)]
