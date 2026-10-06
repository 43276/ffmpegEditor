"""专辑封面与标签写入命令，不执行进程。"""
from __future__ import annotations

from pathlib import Path

from .models import TrackMetadata


def image_codec_for(image_path: Path) -> str:
    if image_path.suffix.lower() in {".jpg", ".jpeg"}:
        return "mjpeg"
    return "png"

def build_album_command(
    ffmpeg: str,
    audio_path: Path,
    image_path: Path,
    output_path: Path,
    overwrite: bool,
    metadata: TrackMetadata,
) -> list[str]:
    """为单个音频构建嵌入封面并写元数据的 ffmpeg 命令。"""
    ext = audio_path.suffix.lower()
    cover_codec = "mjpeg" if ext in {".mp3", ".wav"} else image_codec_for(image_path)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y" if overwrite else "-n",
        "-i",
        str(audio_path),
        "-i",
        str(image_path),
        "-map",
        "0:a?",
        "-map",
        "1:v:0",
        "-map_metadata",
        "0",
        "-metadata",
        f"album={metadata.album}",
        "-metadata",
        f"artist={metadata.artist}",
        "-metadata",
        "title=",
        "-metadata",
        "#=",
        "-c:v",
        cover_codec,
        "-metadata:s:v",
        "title=Album cover",
        "-metadata:s:v",
        "comment=Cover (front)",
    ]

    if ext == ".wav":
        command.extend([
            "-c:a", "libmp3lame", "-q:a", "2", "-id3v2_version", "3",
            "-disposition:v:0", "attached_pic",
        ])
    elif ext == ".mp3":
        # MP3 中的图片流必须标记为 attached_pic，播放器才会将其识别为封面。
        command.extend([
            "-c:a", "copy", "-id3v2_version", "3",
            "-disposition:v:0", "attached_pic",
        ])
    else:
        command.extend(["-c:a", "copy", "-disposition:v:0", "attached_pic"])

    command.append(str(output_path))
    return command
