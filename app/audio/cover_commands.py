"""封面缩略图、导出和转换准备的完整命令；不执行进程。"""
from __future__ import annotations

from pathlib import Path


def cover_suffix_for_codec(codec: str | None) -> str:
    return ".png" if codec == "png" else ".jpg"


def build_cover_thumbnail_command(ffmpeg_path: str, audio_path: Path) -> list[str]:
    return [
        ffmpeg_path, "-hide_banner", "-loglevel", "error", "-i", str(audio_path),
        "-map", "0:v:0", "-frames:v", "1", "-c:v", "mjpeg", "-f", "image2pipe", "-",
    ]


def build_cover_export_command(
    ffmpeg_path: str, audio_path: Path, output_path: Path, cover_codec: str | None,
) -> list[str]:
    return [
        ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y", "-i", str(audio_path),
        "-map", "0:v:0", "-frames:v", "1", "-c:v",
        "copy" if cover_codec in {"mjpeg", "jpeg", "png"} else "mjpeg", str(output_path),
    ]
