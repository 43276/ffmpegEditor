"""音频元数据编辑与格式转换核心逻辑。

约定（与界面一致）：
- 读取用 ffprobe，写入用 ffmpeg；
- 仅元数据修改时复制音频流（-c copy）；指定格式转换时才重编码；
- 封面动作：set=替换、remove=移除、None=保持；
- 原地模式：不转格式时写临时文件后原子替换源文件；转格式且扩展名变化时
  新文件生成在源文件旁并保留源文件；输出模式：全部写入指定输出目录；
- .wav / .aac / .ogg / .opus 容器不支持内嵌封面，写盘时自动忽略封面并给出警告；
- 输出的 mp3 统一写 ID3v2.3（与图片模块 / 专辑批处理保持一致）；
- 输出文件与既有文件重名时自动追加序号，不覆盖已存在的其它文件。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from app.AddCover import AUDIO_EXTENSIONS, IMAGE_EXTENSIONS


class MetadataError(Exception):
    """元数据编辑相关的可预期错误，消息可直接展示给用户。"""


# ---- 字段模型 ---------------------------------------------------------

@dataclass(frozen=True)
class MetadataField:
    key: str
    display_name: str
    kind: str  # "text" | "artist" | "cover"
    default_checked: bool


METADATA_FIELDS: tuple[MetadataField, ...] = (
    MetadataField("cover", "封面", "cover", True),
    MetadataField("title", "标题", "text", True),
    MetadataField("artist", "作者", "artist", True),
    MetadataField("album", "专辑", "text", True),
    MetadataField("album_artist", "专辑艺术家", "text", False),
    MetadataField("date", "年份", "text", False),
    MetadataField("track", "音轨号", "text", False),
    MetadataField("genre", "流派", "text", False),
    MetadataField("comment", "注释", "text", False),
)

TEXT_FIELDS: tuple[MetadataField, ...] = tuple(
    item for item in METADATA_FIELDS if item.kind != "cover"
)

_FIELDS_BY_KEY = {item.key: item for item in METADATA_FIELDS}


def FieldByKey(key: str) -> MetadataField | None:
    return _FIELDS_BY_KEY.get(key)


# ---- 格式转换模型 -----------------------------------------------------

@dataclass(frozen=True)
class AudioFormatOption:
    key: str
    display_name: str
    extension: str | None          # None 表示保持原格式
    lossy: bool
    bitrates: tuple[str, ...] = ()
    cover_capable: bool = False


FORMAT_OPTIONS: tuple[AudioFormatOption, ...] = (
    AudioFormatOption("keep", "保持原格式", None, False, (), False),
    AudioFormatOption("mp3", "MP3", ".mp3", True, ("128k", "192k", "256k", "320k"), True),
    AudioFormatOption("m4a", "M4A (AAC)", ".m4a", True, ("128k", "192k", "256k", "320k"), True),
    AudioFormatOption("flac", "FLAC", ".flac", False, (), True),
    AudioFormatOption("ogg", "OGG (Vorbis)", ".ogg", True, ("96k", "128k", "192k", "256k"), False),
    AudioFormatOption("opus", "OPUS", ".opus", True, ("64k", "96k", "128k", "192k"), False),
    AudioFormatOption("wav", "WAV (PCM)", ".wav", False, (), False),
    AudioFormatOption("aac", "AAC (ADTS)", ".aac", True, ("128k", "192k", "256k", "320k"), False),
)

_FORMATS_BY_KEY = {item.key: item for item in FORMAT_OPTIONS}
DEFAULT_BITRATE = "192k"
CONVERT_FORMATS: tuple[AudioFormatOption, ...] = tuple(
    item for item in FORMAT_OPTIONS if item.key != "keep"
)
WAV_AUTO_CONVERT_TARGETS: tuple[AudioFormatOption, ...] = tuple(
    item for item in FORMAT_OPTIONS if item.cover_capable
)

# 容器不支持内嵌封面的目标扩展名：写盘时自动忽略封面修改并给出警告
COVER_UNSUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {".wav", ".aac", ".ogg", ".opus"}
)

# 容器不保存元数据标签的目标扩展名：元数据修改会被静默丢弃，需显式警告
TAG_UNSUPPORTED_EXTENSIONS: frozenset[str] = frozenset({".aac"})


def FormatByKey(key: str | None) -> AudioFormatOption | None:
    if key is None:
        return None
    return _FORMATS_BY_KEY.get(key)


def FormatDisplay(original_ext: str, target_format_key: str | None, bitrate: str | None) -> str:
    """格式列显示文本：无转换为 .mp3；有转换为 .mp3->.m4a 192k（无损无码率）。"""
    source_ext = original_ext.lower()
    target = FormatByKey(target_format_key)
    if target is None or target.key == "keep":
        return source_ext
    text = f"{source_ext}->{target.extension}"
    if target.lossy:
        text += f" {bitrate or DEFAULT_BITRATE}"
    return text


# ---- 输入收集 ---------------------------------------------------------

def IsAudioFile(path: Path) -> bool:
    """是否为可用作输入的音频文件。

    排除隐藏文件与写盘 / 转换残留的临时文件：临时文件名形如
    `.<名称>.part.<扩展名>`，其后缀仍是音频扩展名，不排除会被当成正常音频导入。
    """
    if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
        return False
    name = path.name.lower()
    if name.startswith("."):
        return False
    return ".part." not in name and not name.endswith(".part")


def CollectAudioFiles(folder: str | Path) -> list[Path]:
    """递归收集一个文件夹内的所有音频文件（按路径排序）。"""
    root = Path(folder)
    if not root.exists():
        raise MetadataError(f"路径不存在：{root}")
    if not root.is_dir():
        raise MetadataError(f"不是文件夹：{root}")
    return sorted((path for path in root.rglob("*") if IsAudioFile(path)),
                  key=lambda item: str(item).lower())


# ---- ffprobe 定位与读取 -----------------------------------------------

def LocateFfprobe(ffmpeg_path: str | None = None) -> str:
    """返回 ffprobe 路径：优先与 ffmpeg 同目录，其次 PATH；找不到抛 MetadataError。"""
    if ffmpeg_path:
        base = Path(ffmpeg_path)
        candidate = base.with_name("ffprobe.exe") if os.name == "nt" else base.with_name("ffprobe")
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("ffprobe")
    if found:
        return found
    raise MetadataError("未找到 ffprobe（需要与 ffmpeg 同目录或在 PATH 中）")


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


def _RunFfprobe(ffprobe_path: str, audio_path: Path) -> tuple[bool, str, str]:
    cmd = [
        ffprobe_path,
        "-v",
        "error",
        "-show_entries",
        "format_tags:stream=codec_type,codec_name:stream_disposition=attached_pic",
        "-of",
        "json",
        str(audio_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except OSError as exc:
        return False, "", f"无法运行 ffprobe：{exc}"
    except subprocess.TimeoutExpired:
        return False, "", "ffprobe 读取超时"
    if result.returncode != 0:
        lines = (result.stderr or "未知错误").strip().splitlines()
        return False, "", (lines[-1] if lines else "ffprobe 读取失败")
    return True, result.stdout, ""


def ReadAudioInfo(ffprobe_path: str, audio_path: Path) -> AudioInfo:
    """读取音频现有元数据与内嵌封面信息。"""
    ok, stdout, error = _RunFfprobe(ffprobe_path, audio_path)
    if not ok:
        return AudioInfo(audio_path, {}, False, None, error)

    try:
        data = json.loads(stdout or "{}")
    except json.JSONDecodeError as exc:
        return AudioInfo(audio_path, {}, False, None, f"ffprobe 输出解析失败：{exc}")

    tags = (data.get("format") or {}).get("tags") or {}
    values: dict[str, str] = {}
    for item in TEXT_FIELDS:
        value = tags.get(item.key)
        if value is not None:
            values[item.key] = str(value).strip()

    has_cover = False
    cover_codec: str | None = None
    for stream in data.get("streams") or []:
        if stream.get("codec_type") != "video":
            continue
        if (stream.get("disposition") or {}).get("attached_pic") == 1:
            has_cover = True
            cover_codec = stream.get("codec_name")
            break

    return AudioInfo(audio_path, values, has_cover, cover_codec)


# ---- 封面提取 ---------------------------------------------------------

def ExtractCoverThumbnail(ffmpeg_path: str, audio_path: Path) -> bytes | None:
    """把内嵌封面重编码为 JPEG 字节流，供表格缩略图使用。"""
    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(audio_path),
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-c:v",
        "mjpeg",
        "-f",
        "image2pipe",
        "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    return result.stdout


def CoverSuffixForCodec(codec: str | None) -> str:
    return ".png" if codec == "png" else ".jpg"


def UniquePathFor(
    directory: Path, stem: str, suffix: str, taken: set[str] | None = None
) -> Path:
    """返回不与既有文件冲突的输出路径（同名时追加 ` (1)`、` (2)`…）。

    不同目录下的同名音频（如 A/song.mp3 与 B/song.mp3）导出封面到同一目录时，
    若固定用 `<名称>.<扩展名>` 会相互覆盖，导致封面张冠李戴。taken 用于同一批
    任务内**已被占用但尚未落盘**的输出路径（小写字符串），否则同一批里的同名
    输出会算出同一个名字。
    """
    candidate = directory / f"{stem}{suffix}"
    counter = 1
    while candidate.exists() or (taken is not None and str(candidate).lower() in taken):
        candidate = directory / f"{stem} ({counter}){suffix}"
        counter += 1
    return candidate


def ExtractCoverToFile(
    ffmpeg_path: str,
    audio_path: Path,
    dest_dir: str | Path,
    cover_codec: str | None,
) -> tuple[Path | None, str | None]:
    """把内嵌封面导出为图片文件（尽量复制原编码），返回 (输出路径, 错误)。"""
    output_dir = Path(dest_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = CoverSuffixForCodec(cover_codec)
    dest = UniquePathFor(output_dir, audio_path.stem, suffix)

    copy_ok = cover_codec in {"mjpeg", "jpeg", "png"}
    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(audio_path),
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-c:v",
        "copy" if copy_ok else "mjpeg",
        str(dest),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except OSError as exc:
        return None, f"无法运行 ffmpeg：{exc}"
    except subprocess.TimeoutExpired:
        return None, "封面导出超时"
    if result.returncode != 0:
        lines = (result.stderr or "未知错误").strip().splitlines()
        return None, (lines[-1] if lines else "封面导出失败")
    return dest, None


# ---- 内存编辑模型 -----------------------------------------------------

@dataclass
class TrackEdit:
    """表格中一行音频的内存态（未确认前不写盘）。"""

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
    def FromAudioInfo(cls, info: AudioInfo) -> "TrackEdit":
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
        return FormatDisplay(self.original_ext, self.target_format_key, self.bitrate)


# ---- 写盘命令构建 -----------------------------------------------------

@dataclass
class OutputPlan:
    output_path: Path
    temp_path: Path
    command: list[str]
    converted: bool
    replaces_source: bool
    warnings: list[str] = field(default_factory=list)


def _AudioEncoderArgs(fmt: AudioFormatOption, bitrate: str | None) -> list[str]:
    if fmt.key == "mp3":
        return ["-c:a", "libmp3lame", "-b:a", bitrate or DEFAULT_BITRATE]
    if fmt.key == "m4a":
        return ["-c:a", "aac", "-b:a", bitrate or DEFAULT_BITRATE]
    if fmt.key == "ogg":
        return ["-c:a", "libvorbis", "-b:a", bitrate or DEFAULT_BITRATE]
    if fmt.key == "opus":
        return ["-c:a", "libopus", "-b:a", bitrate or DEFAULT_BITRATE]
    if fmt.key == "aac":
        return ["-c:a", "aac", "-b:a", bitrate or DEFAULT_BITRATE]
    if fmt.key == "wav":
        return ["-c:a", "pcm_s16le"]
    if fmt.key == "flac":
        return ["-c:a", "flac"]
    return ["-c:a", "copy"]


def _CoverVideoCodec(target_ext: str, cover_source: Path) -> str:
    if target_ext == ".mp3":
        return "mjpeg"
    if cover_source.suffix.lower() == ".png":
        return "png"
    return "mjpeg"


def BuildOutputPlan(
    ffmpeg_path: str,
    edit: TrackEdit,
    overwrite: bool,
    in_place: bool,
    output_root: str | Path | None,
    used_outputs: set[str] | None = None,
) -> OutputPlan:
    """为一行音频构建写盘计划（输出路径、临时路径与 ffmpeg 命令）。

    used_outputs：同一批任务里已被占用的输出路径（小写字符串），用于避免不同源
    目录下的同名文件在输出模式里相互覆盖。
    """
    source = edit.path
    source_ext = source.suffix.lower()
    fmt = FormatByKey(edit.target_format_key)
    converting = fmt is not None and fmt.key != "keep"
    target_ext = fmt.extension if converting else source_ext
    warnings: list[str] = []

    if in_place:
        if target_ext == source_ext:
            output_path = source
            replaces_source = True
        else:
            # 换扩展名：不覆盖源文件旁的既有文件，重名时改用带序号的名字
            desired = source.with_suffix(target_ext)
            if desired.exists() or (
                used_outputs is not None and str(desired).lower() in used_outputs
            ):
                output_path = UniquePathFor(
                    source.parent, source.stem, target_ext, used_outputs
                )
            else:
                output_path = desired
            if output_path != desired:
                warnings.append(f"同名文件已存在，输出改为 {output_path.name}")
            replaces_source = False
    else:
        if output_root is None:
            raise MetadataError("输出模式需要先选择输出目录")
        root = Path(output_root)
        if root.exists() and not root.is_dir():
            raise MetadataError(f"输出路径不是文件夹：{root}")
        desired = root / f"{source.stem}{target_ext}"
        if used_outputs is not None and str(desired).lower() in used_outputs:
            output_path = UniquePathFor(root, source.stem, target_ext, used_outputs)
            warnings.append(f"输出重名，已改为 {output_path.name}")
        else:
            output_path = desired
        replaces_source = False

    if used_outputs is not None and not replaces_source:
        used_outputs.add(str(output_path).lower())

    temp_path = output_path.with_name(f".{output_path.name}.part{output_path.suffix}")

    cover_action = edit.cover_action
    if cover_action == "set" and target_ext in COVER_UNSUPPORTED_EXTENSIONS:
        cover_action = None
        warnings.append(f"{target_ext} 容器不支持内嵌封面，已忽略封面修改")

    edited_values = edit.edited_values
    if edited_values and target_ext in TAG_UNSUPPORTED_EXTENSIONS:
        edited_values = {}
        warnings.append(f"{target_ext} 容器不保存元数据标签，已忽略元数据修改")

    flag = "-y" if (overwrite or replaces_source) else "-n"
    command = [ffmpeg_path, "-hide_banner", "-loglevel", "error", flag, "-i", str(source)]

    if cover_action == "set" and edit.cover_source is not None:
        command += ["-i", str(edit.cover_source)]

    # 元数据：保留原有标签，再覆盖被编辑过的字段（空值 = 删除该标签）
    command += ["-map_metadata", "0"]
    for key, value in edited_values.items():
        command += ["-metadata", f"{key}={value}"]

    if converting:
        command += ["-map", "0:a?"]
        if cover_action == "set" and edit.cover_source is not None:
            command += ["-map", "1:v:0"]
        command += _AudioEncoderArgs(fmt, edit.bitrate)
        if cover_action == "set" and edit.cover_source is not None:
            codec = _CoverVideoCodec(target_ext, edit.cover_source)
            command += [
                "-c:v",
                codec,
                "-disposition:v:0",
                "attached_pic",
                "-metadata:s:v",
                "title=Album cover",
                "-metadata:s:v",
                "comment=Cover (front)",
            ]
    elif cover_action == "set" and edit.cover_source is not None:
        codec = _CoverVideoCodec(target_ext, edit.cover_source)
        command += [
            "-map",
            "0:a?",
            "-map",
            "1:v:0",
            "-c:a",
            "copy",
            "-c:v",
            codec,
            "-disposition:v:0",
            "attached_pic",
            "-metadata:s:v",
            "title=Album cover",
            "-metadata:s:v",
            "comment=Cover (front)",
        ]
    elif cover_action == "remove":
        command += ["-map", "0:a?", "-c:a", "copy"]
    else:
        # 保持原格式且不改封面：复制全部流（含原内嵌封面）
        command += ["-map", "0", "-c", "copy"]

    # mp3 统一写 ID3v2.3：与图片模块 / 专辑批处理一致，Windows 资源管理器兼容更好
    if target_ext == ".mp3":
        command += ["-id3v2_version", "3"]

    command.append(str(temp_path))
    return OutputPlan(
        output_path=output_path,
        temp_path=temp_path,
        command=command,
        converted=converting,
        replaces_source=replaces_source,
        warnings=warnings,
    )
