"""音频元数据编辑与格式转换核心逻辑。

约定（与界面一致）：
- 读取用 ffprobe，写入用 ffmpeg；
- 仅元数据修改时复制音频流（-c copy）；指定格式转换时才重编码；
- 封面动作：set=替换、remove=移除、None=保持；
- 原地模式：不转格式时写临时文件后原子替换源文件；转格式且扩展名变化时
  新文件生成在源文件旁并保留源文件；输出模式：全部写入指定输出目录；
- .wav / .aac / .ogg / .opus 容器不支持内嵌封面，写入时自动忽略封面并给出警告；
- 输出的 mp3 统一写 ID3v2.3（与图片模块 / 专辑批处理保持一致）；
- 输出文件与既有文件重名时自动追加序号，不覆盖已存在的其它文件。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable

from .audio.models import AudioFormatOption, AudioInfo, CoverExportResult, MetadataField, TrackEdit
from .audio.formats import (
    AUDIO_EXTENSIONS, IMAGE_EXTENSIONS, METADATA_FIELDS, TEXT_FIELDS, FORMAT_OPTIONS,
    CONVERT_FORMATS, WAV_AUTO_CONVERT_TARGETS, DEFAULT_BITRATE, COVER_UNSUPPORTED_EXTENSIONS,
    TAG_UNSUPPORTED_EXTENSIONS,
    field_by_key as FieldByKey, format_by_key as FormatByKey, format_display as FormatDisplay,
)
from .errors import FfmpegError
from .ffmpeg_environment import locate_ffprobe
from .output_files import commit_output, output_transaction, path_key, temporary_path_for, unique_path_for
from .task_models import FilePlan, ProcessResult
from .Converter import RunFileProcess

# 过渡类型入口，数据定义归公共模型所有。
OutputPlan = FilePlan


class MetadataError(Exception):
    """元数据编辑相关的可预期错误，消息可直接展示给用户。"""


# ---- 输入收集 ---------------------------------------------------------

def IsAudioFile(path: Path) -> bool:
    """是否为可用作输入的音频文件。

    排除隐藏文件与写入 / 转换残留的临时文件：临时文件名形如
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
    try:
        return locate_ffprobe(ffmpeg_path)
    except FfmpegError as exc:
        raise MetadataError(str(exc)) from exc


def _RunFfprobe(ffprobe_path: str, audio_path: Path) -> ProcessResult:
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
        # ffprobe 的 JSON 是 UTF-8；Windows 的 text=True 默认会按 GBK 解码，
        # 遇到含特殊字符的路径或标签会在后台 reader 线程直接抛 UnicodeDecodeError。
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except OSError as exc:
        return ProcessResult(-1, f"无法运行 ffprobe：{exc}")
    except subprocess.TimeoutExpired:
        return ProcessResult(-1, "ffprobe 读取超时", timed_out=True)
    if result.returncode != 0:
        lines = (result.stderr or "未知错误").strip().splitlines()
        return ProcessResult(result.returncode, lines[-1] if lines else "ffprobe 读取失败")
    return ProcessResult(0, stdout=(result.stdout or "").encode("utf-8"))


def ReadAudioInfo(ffprobe_path: str, audio_path: Path) -> AudioInfo:
    """读取音频现有元数据与内嵌封面信息。"""
    result = _RunFfprobe(ffprobe_path, audio_path)
    if not result.succeeded:
        return AudioInfo(audio_path, {}, False, None, result.stderr)

    try:
        data = json.loads(result.stdout.decode("utf-8", errors="replace") or "{}")
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
    image_codecs = {"mjpeg", "jpeg", "png", "bmp", "webp", "gif", "tiff"}
    for stream in data.get("streams") or []:
        if stream.get("codec_type") != "video":
            continue
        codec = (stream.get("codec_name") or "").lower()
        if (stream.get("disposition") or {}).get("attached_pic") == 1:
            has_cover = True
            cover_codec = stream.get("codec_name")
            break
        # 部分文件的封面流没有 attached_pic 标记（或标记缺失），但音频文件里的
        # 视频流只要是图片编码，就视为内嵌封面。
        if codec in image_codecs and not has_cover:
            has_cover = True
            cover_codec = stream.get("codec_name")

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
        result = subprocess.run(
            cmd, capture_output=True, timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
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
    任务内**已被占用但尚未落盘**的输出路径（由 path_key 生成），否则同一批里的同名
    输出会算出同一个名字。
    """
    return unique_path_for(directory, stem, suffix, taken)


def ExtractCoverToFile(
    ffmpeg_path: str,
    audio_path: Path,
    dest_dir: str | Path,
    cover_codec: str | None,
    *, cancel_check: Callable[[], bool] | None = None,
) -> CoverExportResult:
    """把内嵌封面写入独立临时文件，成功后保存到未占用的正式路径。"""
    output_dir = Path(dest_dir)
    suffix = CoverSuffixForCodec(cover_codec)
    dest = UniquePathFor(output_dir, audio_path.stem, suffix)
    temporary = temporary_path_for(dest)

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
        str(temporary),
    ]
    plan = FilePlan(audio_path, dest, temporary, tuple(cmd), overwrite=False)
    try:
        with output_transaction(plan):
            result = RunFileProcess(plan.command, cancel_check=cancel_check, timeout_seconds=60)
            if result.cancelled or (cancel_check is not None and cancel_check()):
                return CoverExportResult(cancelled=True)
            if not result.succeeded:
                return CoverExportResult(error=result.stderr or "封面导出失败")
            if not commit_output(plan):
                return CoverExportResult(error=f"输出文件已存在：{dest.name}")
    except OSError as exc:
        return CoverExportResult(error=f"无法导出封面：{exc}")
    return CoverExportResult(path=dest)


# ---- 内存编辑模型 -----------------------------------------------------

# ---- 写入命令构建 -----------------------------------------------------

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
    """为一行音频构建写入计划（输出路径、临时路径与 ffmpeg 命令）。

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
                used_outputs is not None and path_key(desired) in used_outputs
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
        if path_key(desired) == path_key(source) or (
            used_outputs is not None and path_key(desired) in used_outputs
        ):
            output_path = UniquePathFor(root, source.stem, target_ext, used_outputs)
            warnings.append(f"输出重名，已改为 {output_path.name}")
        else:
            output_path = desired
        replaces_source = False

    if used_outputs is not None and not replaces_source:
        used_outputs.add(path_key(output_path))

    temp_path = temporary_path_for(output_path)

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
    return FilePlan(
        source_path=source,
        overwrite=overwrite if not in_place else replaces_source,
        backup=edit.backup,
        output_path=output_path,
        temp_path=temp_path,
        command=tuple(command),
        converted=converting,
        replaces_source=replaces_source,
        warnings=tuple(warnings),
    )
