"""核心处理规则：把用户选择的输入路径解析成一批批可执行的处理批次。

规则（已经与用户确认）：
- A：单文件 → 输出到文件所在目录下的 "<文件名去后缀>_output" 文件夹；
- B：目录下没有（真实）子文件夹且自身含图片 → 该目录整体一批，
      输出到 "<目录名>_output" 文件夹，结果整批合并输出；
- C：目录下仍有子文件夹 → 递归检查每个子文件夹，递归到所有满足 B 的
      叶子目录，各叶子目录按 B 处理；
- 已生成的 "*_output" 目录会被忽略，避免把上一次的输出再当成输入。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

OUTPUT_SUFFIX = "_output"

# 支持的图片格式（GIF / WebP 可能为多帧动画，其余均为单帧静态图）
SUPPORTED_EXTENSIONS: set[str] = {
    ".jpg", ".jpeg", ".png", ".webp", ".avif", ".bmp", ".tif", ".tiff", ".gif",
}

# 支持动画（多帧）的格式：GIF ⇄ WebP 之间可以整段互转并保留动画
ANIMATED_EXTENSIONS: set[str] = {".gif", ".webp"}

# GUI“目标格式”下拉选项：(显示文本, 扩展名或 None 表示保持原格式)
TARGET_FORMAT_OPTIONS: list[tuple[str, str | None]] = [
    ("保持原格式（仅压缩 / 重新编码）", None),
    ("JPG / JPEG", ".jpg"),
    ("PNG（无损）", ".png"),
    ("WebP（支持动画）", ".webp"),
    ("GIF（动画 / 256 色）", ".gif"),
    ("AVIF", ".avif"),
    ("BMP（无损）", ".bmp"),
    ("TIFF（无损）", ".tiff"),
]

# 无损目标格式：压缩率与“质量”滑块无关
LOSSLESS_EXTENSIONS: set[str] = {".png", ".bmp", ".tif", ".tiff"}


def IsPictureFile(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS


def IsLosslessExtension(extension: str | None) -> bool:
    return extension is not None and extension.lower() in LOSSLESS_EXTENSIONS


class InputError(Exception):
    """输入路径不符合处理规则时抛出，消息可直接展示给用户。"""


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


def BuildBatchesForInputs(
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
        if any(not IsPictureFile(path) for path in paths):
            invalid = next(path for path in paths if not IsPictureFile(path))
            raise InputError(f"不支持的文件类型：{invalid.name}")
        parents = {path.resolve().parent for path in paths}
        if len(parents) != 1:
            raise InputError("多选文件必须位于同一个文件夹内")

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
        batches.extend(BuildBatches(path, include_output_dirs=include_output_dirs))
    if output_root is not None:
        batches = RelocateBatchOutputs(batches, output_root)
    return batches


def MakeTaskOutputName(started_at: datetime | None = None) -> str:
    """生成多文件任务使用的时间目录名。"""
    moment = started_at or datetime.now()
    return moment.strftime("%Y-%m-%d_%H-%M-%S")


def BuildBatches(
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
        if not IsPictureFile(path):
            raise InputError(f"不支持的文件类型：{path.name}")
        output_dir = path.parent / f"{path.stem}{OUTPUT_SUFFIX}"
        return [Batch(folder=path.parent, output_dir=output_dir, files=[path], base_name=path.stem)]

    if path.is_dir():
        batches: list[Batch] = []
        _CollectBatchesFromFolder(path, batches, include_output_dirs)
        if not batches:
            raise InputError(f"该位置没有找到可处理的图片：{path}")
        return batches

    raise InputError(f"既不是文件也不是文件夹：{path}")


def _CollectBatchesFromFolder(
    folder: Path, batches: list[Batch], include_output_dirs: bool = False
) -> None:
    """递归收集批次。

    排除已生成的 *_output 子目录后（include_output_dirs 为 True 时不排除）：
    - 若没有其它真实子文件夹且目录内含图片 → 按 B 整批处理；
    - 否则 → 对每个真实子文件夹递归（C，递归到所有满足条件的叶子目录）。
    """
    real_sub_dirs: list[Path] = [
        d for d in folder.iterdir()
        if d.is_dir()
        and (include_output_dirs or not d.name.endswith(OUTPUT_SUFFIX))
    ]
    pictures: list[Path] = sorted(
        (f for f in folder.iterdir() if f.is_file() and IsPictureFile(f)),
        key=lambda p: p.name.lower(),
    )

    if real_sub_dirs:
        for sub_dir in real_sub_dirs:
            _CollectBatchesFromFolder(sub_dir, batches, include_output_dirs)
    elif pictures:
        output_dir = folder / f"{folder.name}{OUTPUT_SUFFIX}"
        batches.append(
            Batch(
                folder=folder,
                output_dir=output_dir,
                files=pictures,
                base_name=folder.name,
            )
        )


def RelocateBatchOutputs(batches: list[Batch], output_root: str | Path) -> list[Batch]:
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
        if output_name.lower() in used_dirs:
            counter = 1
            while f"{output_name} ({counter})".lower() in used_dirs:
                counter += 1
            output_name = f"{output_name} ({counter})"
        used_dirs.add(output_name.lower())
        relocated.append(
            Batch(
                folder=batch.folder,
                output_dir=root / output_name,
                files=batch.files,
                base_name=batch.base_name,
            )
        )
    return relocated


def SummarizeBatches(batches: list[Batch]) -> str:
    """生成供界面预览的摘要文本。"""
    total_files = sum(len(batch.files) for batch in batches)
    if len(batches) == 1:
        batch = batches[0]
        if len(batch.files) == 1:
            return f"将处理 1 个文件，输出到：{batch.output_dir}"
        return f"将批量处理 {len(batch.files)} 个文件，输出到：{batch.output_dir}"
    first_dir = batches[0].output_dir
    return (
        f"发现 {len(batches)} 个待处理目录、共 {total_files} 个文件"
        f"（首个输出目录：{first_dir}）"
    )
