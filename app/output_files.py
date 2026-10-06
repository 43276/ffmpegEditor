"""按业务计划管理输出文件；命名与格式规则由调用方决定。"""
from __future__ import annotations

import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from .task_models import FilePlan


def path_key(path: str | Path) -> str:
    """与 Windows 路径比较保持一致，供同一任务的占用集合使用。"""
    return str(Path(path).resolve()).casefold()


def unique_path_for(
    directory: Path, stem: str, suffix: str,
    taken: set[str] | None = None, *, include_existing: bool = True,
) -> Path:
    candidate = directory / f"{stem}{suffix}"
    counter = 1
    while (include_existing and candidate.exists()) or (
        taken is not None and path_key(candidate) in taken
    ):
        candidate = directory / f"{stem} ({counter}){suffix}"
        counter += 1
    return candidate


def reserve_output_path(desired: Path, taken: set[str]) -> Path:
    """规划时只占用名字；既有文件由计划中的覆盖策略决定。"""
    path = unique_path_for(desired.parent, desired.stem, desired.suffix,
                           taken, include_existing=False)
    taken.add(path_key(path))
    return path


def temporary_path_for(output_path: Path) -> Path:
    """同目录保存便于原子提交；保留扩展名供 FFmpeg 选择封装器。"""
    return output_path.with_name(f".{output_path.name}.{uuid4().hex}.part{output_path.suffix}")


def ensure_output_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)


def _validate_paths(plan: FilePlan) -> None:
    source = path_key(plan.source_path)
    output = path_key(plan.output_path)
    temporary = path_key(plan.temp_path)
    if temporary in {source, output} or plan.temp_path.parent.resolve() != plan.output_path.parent.resolve():
        raise ValueError("临时文件必须在输出目录内，且不能是源文件或正式输出")
    if (source == output) != plan.replaces_source:
        raise ValueError("源文件替换策略与输出路径不一致")


def prepare_output(plan: FilePlan) -> None:
    _validate_paths(plan)
    ensure_output_directory(plan.output_path.parent)
    # 不删除另一任务的文件，也不预建空文件影响 FFmpeg 的 -n 行为。
    if plan.temp_path.exists():
        raise FileExistsError(f"临时路径已被占用：{plan.temp_path}")


def cleanup_output(plan: FilePlan) -> None:
    _validate_paths(plan)
    plan.temp_path.unlink(missing_ok=True)


def _backup_source(source: Path) -> None:
    backup = source.with_name(source.name + ".bak")
    temporary = temporary_path_for(backup)
    try:
        shutil.copy2(source, temporary)
        temporary.replace(backup)
    finally:
        temporary.unlink(missing_ok=True)


def commit_output(plan: FilePlan) -> bool:
    """成功保存返回 True；输出在处理期间被占用时返回 False。

    禁止覆盖时使用原子的不覆盖操作，避免检查存在后再替换的竞争窗口。
    备份失败会抛出异常，调用方不得继续替换源文件。
    """
    _validate_paths(plan)
    if not plan.temp_path.is_file():
        raise FileNotFoundError(f"未生成临时输出：{plan.temp_path}")
    if not plan.overwrite and not plan.replaces_source:
        try:
            if os.name == "nt":
                plan.temp_path.rename(plan.output_path)
            else:
                os.link(plan.temp_path, plan.output_path)
                plan.temp_path.unlink()
        except FileExistsError:
            return False
        return True
    if plan.replaces_source and plan.backup:
        _backup_source(plan.source_path)
    plan.temp_path.replace(plan.output_path)
    return True


@contextmanager
def output_transaction(plan: FilePlan) -> Iterator[None]:
    prepare_output(plan)
    try:
        yield
    finally:
        cleanup_output(plan)
