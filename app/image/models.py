"""图片参数与输入批次。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class ConvertOptions:
    ffmpeg_path: str
    target_extension: str | None = None   # None 表示保持原格式
    quality: int | None = 80              # 1~100，仅对有损编码有效
    max_dimension: int = 0                # 0 表示不缩放
    overwrite: bool = True                # 输出文件已存在时是否覆盖


@dataclass
class Batch:
    """一批待处理文件：folder 下的所有 files 统一输出到 output_dir。

    base_name：输出子目录的基础名（不带 _output 后缀）。
    单文件（规则 A）为文件名去后缀；目录整批（规则 B）为文件夹名。
    """

    folder: Path
    output_dir: Path
    files: list[Path]
    base_name: str | None = None
