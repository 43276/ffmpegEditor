"""音频快照、编辑草稿和专辑计划。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class CoverExportResult:
    path: Path | None = None
    error: str | None = None
    cancelled: bool = False


@dataclass(frozen=True)
class MetadataField:
    key: str
    display_name: str
    kind: str  # "text" | "artist" | "cover"
    default_checked: bool


@dataclass(frozen=True)
class AudioFormatOption:
    key: str
    display_name: str
    extension: str | None          # None 表示保持原格式
    lossy: bool
    bitrates: tuple[str, ...] = ()
    cover_capable: bool = False


@dataclass
class AudioInfo:
    """单个音频在磁盘上的元数据快照。"""

    path: Path
    values: dict[str, str]
    has_cover: bool
    cover_codec: str | None
    error: str | None = None

    @property
    def file_name(self) -> str:
        return self.path.name

    @property
    def original_ext(self) -> str:
        return self.path.suffix.lower()


@dataclass
class TrackEdit:
    """表格中一行音频的内存态（未确认前不写入）。"""

    path: Path
    original_ext: str = ""
    original_values: dict[str, str] = field(default_factory=dict)
    edited_values: dict[str, str] = field(default_factory=dict)
    has_original_cover: bool = False
    cover_codec: str | None = None
    thumbnail_bytes: bytes | None = None
    cover_action: str | None = None            # "set" | "remove" | None
    cover_source: Path | None = None           # set 时的图片文件路径
    target_format_key: str | None = None       # None 表示保持原格式
    bitrate: str | None = None
    backup: bool = False
    error: str | None = None

    @classmethod
    def from_audio_info(cls, info: AudioInfo) -> "TrackEdit":
        return cls(
            path=info.path,
            original_ext=info.original_ext,
            original_values=dict(info.values),
            has_original_cover=info.has_cover,
            cover_codec=info.cover_codec,
            error=info.error,
        )

    @property
    def file_name(self) -> str:
        return self.path.name

    @property
    def modified(self) -> bool:
        return bool(self.edited_values) or self.cover_action is not None or (
            self.target_format_key is not None
        )

    @property
    def format_display(self) -> str:
        from .formats import format_display

        return format_display(self.original_ext, self.target_format_key, self.bitrate)


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
