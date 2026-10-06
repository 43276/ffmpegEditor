"""音频编码、封面流映射与标签写入的完整命令。"""
from __future__ import annotations

from pathlib import Path

from .formats import DEFAULT_BITRATE
from .models import AudioFormatOption


def audio_encoder_args(fmt: AudioFormatOption, bitrate: str | None) -> list[str]:
    if fmt.key == "mp3":
        return ["-c:a", "libmp3lame", "-b:a", bitrate or DEFAULT_BITRATE]
    if fmt.key == "m4a":
        return ["-c:a", "aac", "-b:a", bitrate or DEFAULT_BITRATE]
    if fmt.key == "ogg":
        return ["-c:a", "libvorbis", "-b:a", bitrate or DEFAULT_BITRATE]
    if fmt.key == "opus":
        return ["-c:a", "libopus", "-b:a", bitrate or DEFAULT_BITRATE]
    if fmt.key == "aac":
        return ["-c:a", "aac", "-b:a", bitrate or DEFAULT_BITRATE]
    if fmt.key == "wav":
        return ["-c:a", "pcm_s16le"]
    if fmt.key == "flac":
        return ["-c:a", "flac"]
    return ["-c:a", "copy"]


def cover_video_codec(target_ext: str, cover_source: Path) -> str:
    if target_ext == ".mp3":
        return "mjpeg"
    if cover_source.suffix.lower() == ".png":
        return "png"
    return "mjpeg"


def build_metadata_command(
    ffmpeg_path: str, source: Path, output_path: Path, *,
    target_ext: str, fmt: AudioFormatOption | None, bitrate: str | None,
    cover_action: str | None, cover_source: Path | None,
    edited_values: dict[str, str], overwrite: bool,
) -> list[str]:
    converting = fmt is not None and fmt.key != "keep"
    flag = "-y" if overwrite else "-n"
    command = [ffmpeg_path, "-hide_banner", "-loglevel", "error", flag, "-i", str(source)]

    if cover_action == "set" and cover_source is not None:
        command += ["-i", str(cover_source)]

    # 元数据：保留原有标签，再覆盖被编辑过的字段（空值 = 删除该标签）
    command += ["-map_metadata", "0"]
    for key, value in edited_values.items():
        command += ["-metadata", f"{key}={value}"]

    if converting:
        command += ["-map", "0:a?"]
        if cover_action == "set" and cover_source is not None:
            command += ["-map", "1:v:0"]
        command += audio_encoder_args(fmt, bitrate)
        if cover_action == "set" and cover_source is not None:
            codec = cover_video_codec(target_ext, cover_source)
            command += [
                "-c:v",
                codec,
                "-disposition:v:0",
                "attached_pic",
                "-metadata:s:v",
                "title=Album cover",
                "-metadata:s:v",
                "comment=Cover (front)",
            ]
    elif cover_action == "set" and cover_source is not None:
        codec = cover_video_codec(target_ext, cover_source)
        command += [
            "-map",
            "0:a?",
            "-map",
            "1:v:0",
            "-c:a",
            "copy",
            "-c:v",
            codec,
            "-disposition:v:0",
            "attached_pic",
            "-metadata:s:v",
            "title=Album cover",
            "-metadata:s:v",
            "comment=Cover (front)",
        ]
    elif cover_action == "remove":
        command += ["-map", "0:a?", "-c:a", "copy"]
    else:
        # 保持原格式且不改封面：复制全部流（含原内嵌封面）
        command += ["-map", "0", "-c", "copy"]

    # mp3 统一写 ID3v2.3：与图片模块 / 专辑批处理一致，Windows 资源管理器兼容更好
    if target_ext == ".mp3":
        command += ["-id3v2_version", "3"]

    command.append(str(output_path))
    return command
