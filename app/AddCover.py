"""专辑封面 / 元数据批处理核心逻辑（移植自原 addCover 脚本）。

规则：
- 输入一个根目录，根目录下每个一级子文件夹视为一张“专辑”；
- 在专辑文件夹内递归查找音频和图片；
- 专辑内没有图片、有多张图片，或封面所在目录缺少 / 为空的
  album.txt、artist.txt 时，跳过该专辑；
- 用找到的唯一图片作为封面，写入 album / artist，并清空 title 与 #；
- 输出到每个音频实际所在目录下的 `<专辑文件夹名>` 子目录；
- .wav 会转码为 .mp3，其余格式复制原音频流并尽量保持扩展名。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

AUDIO_EXTENSIONS = {
    ".mp3",
    ".m4a",
    ".mp4",
    ".aac",
    ".flac",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
    ".alac",
}

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
}


class AlbumCoverError(Exception):
    """专辑封面处理相关的可预期错误，消息可直接展示给用户。"""


@dataclass(frozen=True)
class TrackMetadata:
    album: str
    artist: str


@dataclass
class AlbumAudioGroup:
    """同一音频目录下的一批音频，统一输出到 output_dir。"""

    audio_dir: Path
    output_dir: Path
    files: list[Path]


@dataclass
class AlbumTask:
    """一张专辑的处理任务：一张封面 + 元数据 + 若干音频分组。"""

    processing_dir: Path
    image_path: Path
    metadata: TrackMetadata
    groups: list[AlbumAudioGroup]

    @property
    def file_count(self) -> int:
        return sum(len(group.files) for group in self.groups)


@dataclass
class AlbumSkippedDir:
    """被跳过的专辑文件夹及原因。"""

    processing_dir: Path
    reason: str


@dataclass
class AlbumPlan:
    """专辑批处理计划：可处理任务 + 被跳过的文件夹。"""

    tasks: list[AlbumTask]
    skipped_dirs: list[AlbumSkippedDir]

    @property
    def file_count(self) -> int:
        return sum(task.file_count for task in self.tasks)


def ReadTextValue(path: Path) -> str:
    """读取单行文本文件（album.txt 等），缺失或为空时抛出异常。"""
    if not path.is_file():
        raise FileNotFoundError(f"缺少文件: {path}")
    value = path.read_text(encoding="utf-8-sig").strip()
    value = " ".join(value.split())
    if not value:
        raise ValueError(f"文件内容为空: {path}")
    return value


def ReadArtistValue(path: Path) -> str:
    """读取 artist.txt，多个艺术家用空白分隔，写入时转为分号分隔。"""
    if not path.is_file():
        raise FileNotFoundError(f"缺少文件: {path}")
    artists = path.read_text(encoding="utf-8-sig").split()
    if not artists:
        raise ValueError(f"文件内容为空: {path}")
    return ";".join(artists)


def ReadMetadataNearImage(image_path: Path) -> TrackMetadata:
    """从封面图片所在目录读取 album.txt 与 artist.txt。"""
    metadata_dir = image_path.parent
    return TrackMetadata(
        album=ReadTextValue(metadata_dir / "album.txt"),
        artist=ReadArtistValue(metadata_dir / "artist.txt"),
    )


def ImageCodecFor(image_path: Path) -> str:
    if image_path.suffix.lower() in {".jpg", ".jpeg"}:
        return "mjpeg"
    return "png"


def IsAudioFile(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS


def IsImageFile(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS


def OutputPathFor(audio_dir: Path, output_dir: Path, audio_path: Path) -> Path:
    """输出路径：相对音频目录的结构平移到输出目录；.wav 转为 .mp3。"""
    relative_path = audio_path.relative_to(audio_dir)
    if audio_path.suffix.lower() == ".wav":
        relative_path = relative_path.with_suffix(".mp3")
    return output_dir / relative_path


def BuildFfmpegCommand(
    ffmpeg: str,
    audio_path: Path,
    image_path: Path,
    output_path: Path,
    overwrite: bool,
    metadata: TrackMetadata,
) -> list[str]:
    """为单个音频构建嵌入封面并写元数据的 ffmpeg 命令。"""
    ext = audio_path.suffix.lower()
    cover_codec = "mjpeg" if ext in {".mp3", ".wav"} else ImageCodecFor(image_path)
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
        command.extend(["-c:a", "libmp3lame", "-q:a", "2", "-id3v2_version", "3"])
    elif ext == ".mp3":
        command.extend(["-c:a", "copy", "-id3v2_version", "3"])
    else:
        command.extend(["-c:a", "copy", "-disposition:v:0", "attached_pic"])

    command.append(str(output_path))
    return command


def IsInGeneratedOutput(path: Path, processing_dir: Path) -> bool:
    """判断路径是否位于专辑内已生成的 `<专辑名>` 输出目录中。"""
    for parent in path.parents:
        if parent == processing_dir:
            return False
        if parent.name == processing_dir.name:
            return True
    return False


def CollectRecursiveAudioFiles(processing_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in processing_dir.rglob("*")
        if IsAudioFile(path) and not IsInGeneratedOutput(path, processing_dir)
    )


def CollectRecursiveImageFiles(processing_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in processing_dir.rglob("*")
        if IsImageFile(path) and not IsInGeneratedOutput(path, processing_dir)
    )


def FindProcessingDirs(root_dir: Path) -> list[Path]:
    """根目录下的每个一级子文件夹视为一张待处理的专辑。"""
    return sorted(path for path in root_dir.iterdir() if path.is_dir())


def GroupAudioByParent(audio_files: list[Path]) -> dict[Path, list[Path]]:
    """把音频按实际所在目录分组，输出目录由各组自己维护。"""
    grouped: dict[Path, list[Path]] = {}
    for audio_file in audio_files:
        grouped.setdefault(audio_file.parent, []).append(audio_file)
    return dict(sorted(grouped.items()))


def BuildAlbumPlan(root_dir: str | Path) -> AlbumPlan:
    """扫描根目录，产出专辑批处理计划。"""
    root = Path(root_dir)
    if not root.exists():
        raise FileNotFoundError(f"根目录不存在: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"不是文件夹: {root}")

    processing_dirs = FindProcessingDirs(root)
    tasks: list[AlbumTask] = []
    skipped_dirs: list[AlbumSkippedDir] = []

    for processing_dir in processing_dirs:
        audio_files = CollectRecursiveAudioFiles(processing_dir)
        image_files = CollectRecursiveImageFiles(processing_dir)

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
            metadata = ReadMetadataNearImage(image_path)
        except (FileNotFoundError, ValueError) as error:
            skipped_dirs.append(AlbumSkippedDir(processing_dir, str(error)))
            continue

        groups = [
            AlbumAudioGroup(
                audio_dir=audio_dir,
                output_dir=audio_dir / processing_dir.name,
                files=files,
            )
            for audio_dir, files in GroupAudioByParent(audio_files).items()
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


def SummarizeAlbumPlan(plan: AlbumPlan) -> str:
    """生成供界面预览的摘要文本。"""
    total_files = plan.file_count
    skipped_count = len(plan.skipped_dirs)

    if not plan.tasks:
        if not plan.skipped_dirs:
            return "根目录下没有找到待处理的一级子文件夹"
        return f"未发现可处理专辑：{skipped_count} 个文件夹将被跳过"

    text = f"发现 {len(plan.tasks)} 张专辑、共 {total_files} 个音频"
    if skipped_count:
        text += f"，另有 {skipped_count} 个文件夹将被跳过"
    first_output = plan.tasks[0].groups[0].output_dir
    return text + f"（首个输出目录：{first_output}）"
