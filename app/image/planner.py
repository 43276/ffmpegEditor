"""图片输入解析与输出规划；规划期间不创建文件。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .formats import SUPPORTED_EXTENSIONS
from .models import Batch, ConvertOptions
from .commands import build_image_command
from app.ffmpeg_environment import FfmpegCapabilities
from app.errors import FfmpegError
from app.input_paths import collect_leaf_media
from app.output_files import path_key, reserve_output_path, temporary_path_for, unique_path_for
from app.task_models import BatchPlan, FilePlan, LogEvent, LogLevel, TaskPlan

OUTPUT_SUFFIX = "_output"


def is_picture_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS


class InputError(Exception):
    """输入路径不符合处理规则时抛出，消息可直接展示给用户。"""


def build_batches_for_inputs(
    input_paths: list[str | Path],
    include_output_dirs: bool = False,
    output_root: str | Path | None = None,
    multi_file_output_name: str | None = None,
) -> list[Batch]:
    """把一组同类输入统一解析为批次。

    输入只能是文件或文件夹中的一种。多个文件必须位于同一层级，
    并作为一个批次处理；多个文件夹则各自按 A/B/C 规则处理。
    """
    paths = [Path(path) for path in input_paths]
    if not paths:
        raise InputError("请先选择输入文件或文件夹")
    missing = next((path for path in paths if not path.exists()), None)
    if missing is not None:
        raise InputError(f"路径不存在：{missing}")

    file_flags = [path.is_file() for path in paths]
    dir_flags = [path.is_dir() for path in paths]
    if not all(file_flags) and not all(dir_flags):
        raise InputError("不能同时选择文件和文件夹")

    if all(file_flags):
        if any(not is_picture_file(path) for path in paths):
            invalid = next(path for path in paths if not is_picture_file(path))
            raise InputError(f"不支持的文件类型：{invalid.name}")
        parents = {path.resolve().parent for path in paths}
        if len(parents) != 1:
            raise InputError("多选文件必须位于同一个文件夹内")

        if len(paths) == 1:
            batches = build_batches(paths[0], include_output_dirs=include_output_dirs)
            return relocate_batch_outputs(batches, output_root) if output_root is not None else batches

        parent = paths[0].parent
        root = Path(output_root) if output_root is not None else parent
        if root.exists() and not root.is_dir():
            raise InputError(f"输出路径不是文件夹：{root}")
        output_name = multi_file_output_name or "<任务开始时间>"
        return [
            Batch(
                folder=parent,
                output_dir=root / output_name,
                files=sorted(paths, key=lambda path: path.name.lower()),
                base_name=output_name,
            )
        ]

    if len(paths) > 1:
        parents = {path.resolve().parent for path in paths}
        if len(parents) != 1:
            raise InputError("多选文件夹必须位于同一个上级文件夹内")

    batches: list[Batch] = []
    for path in paths:
        batches.extend(build_batches(path, include_output_dirs=include_output_dirs))
    if output_root is not None:
        batches = relocate_batch_outputs(batches, output_root)
    return batches


def make_task_output_name(started_at: datetime | None = None) -> str:
    """生成多文件任务使用的时间目录名。"""
    moment = started_at or datetime.now()
    return moment.strftime("%Y-%m-%d_%H-%M-%S")


def build_batches(
    input_path: str | Path, include_output_dirs: bool = False
) -> list[Batch]:
    """按 A/B/C 规则把输入路径解析为处理批次；无可处理内容时抛 InputError。

    include_output_dirs：默认忽略已生成的 *_output 目录（防止把上次输出
    再次当作输入）；置 True 时把它们当作普通目录一并纳入处理。
    """
    path = Path(input_path)
    if not path.exists():
        raise InputError(f"路径不存在：{path}")

    if path.is_file():
        if not is_picture_file(path):
            raise InputError(f"不支持的文件类型：{path.name}")
        output_dir = path.parent / f"{path.stem}{OUTPUT_SUFFIX}"
        return [Batch(folder=path.parent, output_dir=output_dir, files=[path], base_name=path.stem)]

    if path.is_dir():
        batches: list[Batch] = []
        _collect_batches_from_folder(path, batches, include_output_dirs)
        if not batches:
            raise InputError(f"该位置没有找到可处理的图片：{path}")
        return batches

    raise InputError(f"既不是文件也不是文件夹：{path}")


def _collect_batches_from_folder(
    folder: Path, batches: list[Batch], include_output_dirs: bool = False
) -> None:
    for leaf, pictures in collect_leaf_media(
        folder, is_picture_file, include_output_dirs=include_output_dirs,
    ):
        batches.append(
            Batch(
                folder=leaf,
                output_dir=leaf / f"{leaf.name}{OUTPUT_SUFFIX}",
                files=pictures,
                base_name=leaf.name,
            )
        )


def relocate_batch_outputs(batches: list[Batch], output_root: str | Path) -> list[Batch]:
    """把批次输出重定位到指定输出根目录。

    每个批次仍输出到自己的 "<基础名>_output" 子目录，只是这些子目录统一
    创建在 output_root 下（命名规则不变）。同一任务内若出现同名的输出
    子目录（例如不同父目录下的同名源文件夹），自动追加序号 (1)、(2)……
    """
    root = Path(output_root)
    if root.exists() and not root.is_dir():
        raise InputError(f"输出路径不是文件夹：{root}")

    used_dirs: set[str] = set()
    relocated: list[Batch] = []
    for batch in batches:
        base = batch.base_name or batch.folder.name
        output_name = f"{base}{OUTPUT_SUFFIX}"
        output_path = unique_path_for(root, output_name, "", used_dirs, include_existing=False)
        used_dirs.add(path_key(output_path))
        relocated.append(
            Batch(
                folder=batch.folder,
                output_dir=output_path,
                files=batch.files,
                base_name=batch.base_name,
            )
        )
    return relocated


def output_name_for(source: Path, target_extension: str | None) -> str:
    extension = target_extension.lower() if target_extension else source.suffix
    return f"{source.stem}{extension}"


def build_image_task(
    batches: list[Batch], options: ConvertOptions, capabilities: FfmpegCapabilities,
) -> TaskPlan:
    taken = {path_key(source) for batch in batches for source in batch.files}
    planned_batches: list[BatchPlan] = []
    started_name = make_task_output_name()
    for batch in batches:
        output_dir = batch.output_dir
        if output_dir.name == "<任务开始时间>":
            output_dir = output_dir.with_name(started_name)
        files: list[FilePlan] = []
        for source in batch.files:
            destination = reserve_output_path(
                output_dir / output_name_for(source, options.target_extension), taken,
            )
            temporary = temporary_path_for(destination)
            command: tuple[str, ...] = ()
            error = None
            try:
                command = tuple(build_image_command(options, capabilities, source, temporary))
            except (FfmpegError, ValueError) as exc:
                error = str(exc)
            files.append(FilePlan(source, destination, temporary, command,
                                  overwrite=options.overwrite, error=error))
        planned_batches.append(BatchPlan(
            batch.folder.name or str(batch.folder), tuple(files),
            (LogEvent(LogLevel.INFO, f"批次 {batch.folder.name or batch.folder}："
                      f"{len(files)} 个文件 → {output_dir}"),),
        ))
    return TaskPlan(tuple(planned_batches))
