"""专辑扫描、文本读取与输出规划；不创建输出文件。"""
from __future__ import annotations

from pathlib import Path

from .formats import AUDIO_EXTENSIONS, IMAGE_EXTENSIONS
from .models import AlbumAudioGroup, AlbumPlan, AlbumSkippedDir, AlbumTask, TrackMetadata
from .album_commands import build_album_command
from app.output_files import path_key, reserve_output_path, temporary_path_for
from app.task_models import BatchPlan, FilePlan, LogEvent, LogLevel, TaskPlan


class AlbumCoverError(Exception):
    """专辑封面处理相关的可预期错误，消息可直接展示给用户。"""


def read_text_value(path: Path) -> str:
    """读取单行文本文件（album.txt 等），缺失或为空时抛出异常。"""
    if not path.is_file():
        raise FileNotFoundError(f"缺少文件: {path}")
    value = path.read_text(encoding="utf-8-sig").strip()
    value = " ".join(value.split())
    if not value:
        raise ValueError(f"文件内容为空: {path}")
    return value


def read_artist_value(path: Path) -> str:
    """读取 artist.txt，多个艺术家用空白分隔，写入时转为分号分隔。"""
    if not path.is_file():
        raise FileNotFoundError(f"缺少文件: {path}")
    artists = path.read_text(encoding="utf-8-sig").split()
    if not artists:
        raise ValueError(f"文件内容为空: {path}")
    return ";".join(artists)


def read_metadata_near_image(image_path: Path) -> TrackMetadata:
    """从封面图片所在目录读取 album.txt 与 artist.txt。"""
    metadata_dir = image_path.parent
    return TrackMetadata(
        album=read_text_value(metadata_dir / "album.txt"),
        artist=read_artist_value(metadata_dir / "artist.txt"),
    )

def is_album_audio_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS


def is_album_image_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS


def output_path_for(audio_dir: Path, output_dir: Path, audio_path: Path) -> Path:
    """输出路径：相对音频目录的结构平移到输出目录；.wav 转为 .mp3。"""
    relative_path = audio_path.relative_to(audio_dir)
    if audio_path.suffix.lower() == ".wav":
        relative_path = relative_path.with_suffix(".mp3")
    return output_dir / relative_path

def is_in_generated_output(path: Path, processing_dir: Path) -> bool:
    """判断路径是否位于专辑内已生成的 `<专辑名>` 输出目录中。"""
    for parent in path.parents:
        if parent == processing_dir:
            return False
        if parent.name == processing_dir.name:
            return True
    return False


def collect_recursive_audio_files(processing_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in processing_dir.rglob("*")
        if is_album_audio_file(path) and not is_in_generated_output(path, processing_dir)
    )


def collect_recursive_image_files(processing_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in processing_dir.rglob("*")
        if is_album_image_file(path) and not is_in_generated_output(path, processing_dir)
    )


def find_processing_dirs(root_dir: Path) -> list[Path]:
    """根目录下的每个一级子文件夹视为一张待处理的专辑。"""
    return sorted(path for path in root_dir.iterdir() if path.is_dir())


def group_audio_by_parent(audio_files: list[Path]) -> dict[Path, list[Path]]:
    """把音频按实际所在目录分组，输出目录由各组自己维护。"""
    grouped: dict[Path, list[Path]] = {}
    for audio_file in audio_files:
        grouped.setdefault(audio_file.parent, []).append(audio_file)
    return dict(sorted(grouped.items()))


def build_album_plan(root_dir: str | Path) -> AlbumPlan:
    """扫描根目录，产出专辑批处理计划。"""
    root = Path(root_dir)
    if not root.exists():
        raise FileNotFoundError(f"根目录不存在: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"不是文件夹: {root}")

    processing_dirs = find_processing_dirs(root)
    tasks: list[AlbumTask] = []
    skipped_dirs: list[AlbumSkippedDir] = []

    for processing_dir in processing_dirs:
        audio_files = collect_recursive_audio_files(processing_dir)
        image_files = collect_recursive_image_files(processing_dir)

        if not audio_files:
            skipped_dirs.append(AlbumSkippedDir(processing_dir, "没有找到音频文件"))
            continue
        if not image_files:
            skipped_dirs.append(AlbumSkippedDir(processing_dir, "没有找到图片"))
            continue
        if len(image_files) > 1:
            details = "、".join(
                str(image_file.relative_to(processing_dir)) for image_file in image_files[:5]
            )
            if len(image_files) > 5:
                details += f" 等 {len(image_files)} 张"
            skipped_dirs.append(AlbumSkippedDir(processing_dir, f"检测到多张图片：{details}"))
            continue

        image_path = image_files[0]
        try:
            metadata = read_metadata_near_image(image_path)
        except (FileNotFoundError, ValueError) as error:
            skipped_dirs.append(AlbumSkippedDir(processing_dir, str(error)))
            continue

        groups = [
            AlbumAudioGroup(
                audio_dir=audio_dir,
                output_dir=audio_dir / processing_dir.name,
                files=files,
            )
            for audio_dir, files in group_audio_by_parent(audio_files).items()
        ]
        tasks.append(
            AlbumTask(
                processing_dir=processing_dir,
                image_path=image_path,
                metadata=metadata,
                groups=groups,
            )
        )

    return AlbumPlan(tasks=tasks, skipped_dirs=skipped_dirs)


def build_album_task(plan: AlbumPlan, ffmpeg_path: str, overwrite: bool) -> TaskPlan:
    taken = {path_key(source) for task in plan.tasks for group in task.groups for source in group.files}
    batches: list[BatchPlan] = []
    for task in plan.tasks:
        files: list[FilePlan] = []
        for group in task.groups:
            for source in group.files:
                destination = reserve_output_path(
                    output_path_for(group.audio_dir, group.output_dir, source), taken,
                )
                temporary = temporary_path_for(destination)
                command = build_album_command(
                    ffmpeg_path, source, task.image_path, temporary, overwrite, task.metadata,
                )
                files.append(FilePlan(source, destination, temporary, tuple(command), overwrite=overwrite))
        # 一张专辑的所有目录属于同一批次，结束请求不能在目录之间截断。
        batches.append(BatchPlan(
            task.processing_dir.name, tuple(files),
            (LogEvent(LogLevel.INFO, f"专辑 {task.processing_dir.name}：{task.file_count} 个音频，"
                      f"封面 {task.image_path.name}"),),
        ))
    return TaskPlan(
        tuple(batches),
        tuple(LogEvent(LogLevel.WARN, f"[跳过文件夹] {item.processing_dir.name}：{item.reason}")
              for item in plan.skipped_dirs),
        skipped_dirs=len(plan.skipped_dirs),
    )
