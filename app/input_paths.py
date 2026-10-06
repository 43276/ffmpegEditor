"""图片和视频共用的叶子目录遍历机制。"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterator


def collect_leaf_media(
    folder: Path, is_media_file: Callable[[Path], bool], *,
    include_output_dirs: bool = False, output_suffix: str = "_output",
) -> Iterator[tuple[Path, list[Path]]]:
    """忽略生成目录后，只收集没有其他子目录的媒体目录。"""
    entries = list(folder.iterdir())
    children = [
        entry for entry in entries if entry.is_dir()
        and (include_output_dirs or not entry.name.endswith(output_suffix))
    ]
    if children:
        for child in children:
            yield from collect_leaf_media(
                child, is_media_file, include_output_dirs=include_output_dirs,
                output_suffix=output_suffix,
            )
    else:
        files = sorted((entry for entry in entries if is_media_file(entry)),
                       key=lambda entry: entry.name.lower())
        if files:
            yield folder, files
