"""视频参数与输入批次。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class VideoBatch:
    folder: Path
    output_dir: Path
    files: list[Path]


@dataclass
class VideoCompressOptions:
    ffmpeg_path: str
    encoder: str = "libx264"
    crf: int = 23
    preset: str = "medium"
    audio_bitrate: str = "192k"
    overwrite: bool = True
