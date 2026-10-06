"""中立的输入去重、同级校验与可取消目录遍历。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Iterator


class ScanCancelled(Exception):
    """后台输入扫描收到取消请求；不计为业务失败。"""


def check_scan_cancelled(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise ScanCancelled("输入扫描已取消")


def unique_input_paths(paths, *, cancel_check=None) -> list[Path]:
    result, seen = [], set()
    for value in paths:
        check_scan_cancelled(cancel_check)
        path = Path(value)
        key = os.path.normcase(str(path.resolve()))
        if key not in seen:
            result.append(path)
            seen.add(key)
    return result


def validate_inputs(paths, error_type, *, empty_message, same_parent_message,
                    cancel_check=None) -> list[Path]:
    """媒体种类由调用方校验；共用输入类型、存在性及同级规则。"""
    paths = unique_input_paths(paths, cancel_check=cancel_check)
    if not paths:
        raise error_type(empty_message)
    for path in paths:
        check_scan_cancelled(cancel_check)
        if not path.exists():
            raise error_type(f"路径不存在：{path}")
    all_files = all(path.is_file() for path in paths)
    all_dirs = all(path.is_dir() for path in paths)
    if not all_files and not all_dirs:
        raise error_type("不能同时选择文件和文件夹")
    if len(paths) > 1 and len({path.resolve().parent for path in paths}) != 1:
        message = same_parent_message if all_files else "多选文件夹必须位于同一个上级文件夹内"
        raise error_type(message)
    return paths


def directory_entries(folder: Path, *, cancel_check=None) -> list[Path]:
    entries = []
    for entry in folder.iterdir():
        check_scan_cancelled(cancel_check)
        entries.append(entry)
    check_scan_cancelled(cancel_check)
    return entries


def walk_files(folder: Path, *, cancel_check=None, skip_dir=None) -> Iterator[Path]:
    """不跟随目录符号链接，允许业务决定跳过哪些生成目录。"""
    pending = [folder]
    while pending:
        current = pending.pop()
        try:
            entries = directory_entries(current, cancel_check=cancel_check)
        except OSError:
            # 与原 rglob 一致：无法读取的子目录不阻止其余音频导入。
            if current == folder:
                raise
            continue
        for entry in entries:
            check_scan_cancelled(cancel_check)
            if entry.is_dir():
                if not entry.is_symlink() and (skip_dir is None or not skip_dir(entry)):
                    pending.append(entry)
            else:
                yield entry


def collect_leaf_media(
    folder: Path, is_media_file: Callable[[Path], bool], *,
    include_output_dirs: bool = False, output_suffix: str = "_output",
    cancel_check: Callable[[], bool] | None = None,
) -> Iterator[tuple[Path, list[Path]]]:
    """忽略生成目录后，只收集没有其他子目录的媒体目录。"""
    entries = directory_entries(folder, cancel_check=cancel_check)
    children = [
        entry for entry in entries if entry.is_dir()
        and (include_output_dirs or not entry.name.endswith(output_suffix))
    ]
    if children:
        for child in children:
            check_scan_cancelled(cancel_check)
            yield from collect_leaf_media(
                child, is_media_file, include_output_dirs=include_output_dirs,
                output_suffix=output_suffix, cancel_check=cancel_check,
            )
    else:
        files = []
        for entry in entries:
            check_scan_cancelled(cancel_check)
            if is_media_file(entry):
                files.append(entry)
        files = sorted(files,
                       key=lambda entry: entry.name.lower())
        if files:
            yield folder, files
