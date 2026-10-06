"""音频编辑校验、输出规划和保留原封面的多步计划。"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .cover_commands import build_cover_export_command, cover_suffix_for_codec
from .formats import (
    COVER_UNSUPPORTED_EXTENSIONS, DEFAULT_BITRATE, TAG_UNSUPPORTED_EXTENSIONS, format_by_key,
)
from .metadata_commands import build_metadata_command
from .models import TrackEdit
from app.output_files import path_key, temporary_path_for, unique_path_for
from app.task_models import BatchPlan, FilePlan, ProcessStep, TaskPlan


class MetadataError(Exception):
    """可以直接展示给用户的编辑校验错误。"""


def apply_cover_edit(
    edit: TrackEdit, action: str, cover_source: Path | None, *,
    auto_convert_wav: bool = False, wav_target_key: str | None = None,
) -> TrackEdit:
    """返回新的内存草稿；WAV 自动转格式规则由应用层决定。"""
    if action not in {"set", "remove"}:
        raise MetadataError(f"不支持的封面操作：{action}")
    updated = replace(edit, cover_action=action,
                      cover_source=cover_source if action == "set" else None)
    if action == "set" and edit.original_ext == ".wav" and edit.target_format_key is None and auto_convert_wav:
        target = format_by_key(wav_target_key)
        if target is None or not target.cover_capable:
            raise MetadataError("WAV 添加封面需要选择支持封面的转换格式")
        updated.target_format_key = target.key
        updated.bitrate = DEFAULT_BITRATE
    return updated


def build_output_plan(
    ffmpeg_path: str, edit: TrackEdit, overwrite: bool, in_place: bool,
    output_root: str | Path | None, used_outputs: set[str] | None = None,
) -> FilePlan:
    source = edit.path
    if edit.error:
        raise MetadataError(edit.error)
    if not source.is_file():
        raise MetadataError(f"音频文件不存在：{source}")
    source_ext = source.suffix.lower()
    fmt = format_by_key(edit.target_format_key)
    if edit.target_format_key is not None and fmt is None:
        raise MetadataError(f"不支持的目标格式：{edit.target_format_key}")
    if edit.cover_action not in {None, "set", "remove"}:
        raise MetadataError(f"不支持的封面操作：{edit.cover_action}")
    converting = fmt is not None and fmt.key != "keep"
    target_ext = fmt.extension if converting else source_ext
    warnings: list[str] = []

    if in_place:
        if target_ext == source_ext:
            output_path, replaces_source = source, True
        else:
            desired = source.with_suffix(target_ext)
            output_path = unique_path_for(source.parent, source.stem, target_ext, used_outputs)
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
            output_path = unique_path_for(root, source.stem, target_ext, used_outputs)
            warnings.append(f"输出重名，已改为 {output_path.name}")
        else:
            output_path = desired
        replaces_source = False

    if used_outputs is not None and not replaces_source:
        used_outputs.add(path_key(output_path))
    temporary = temporary_path_for(output_path)
    cover_action, cover_source = edit.cover_action, edit.cover_source
    if cover_action == "set" and target_ext in COVER_UNSUPPORTED_EXTENSIONS:
        cover_action, cover_source = None, None
        warnings.append(f"{target_ext} 容器不支持内嵌封面，已忽略封面修改")
    elif cover_action == "set" and (cover_source is None or not cover_source.is_file()):
        raise MetadataError("封面图片不存在，请重新选择")
    edited_values = dict(edit.edited_values)
    if edited_values and target_ext in TAG_UNSUPPORTED_EXTENSIONS:
        edited_values = {}
        warnings.append(f"{target_ext} 容器不保存元数据标签，已忽略元数据修改")

    def command_for(action: str | None, cover: Path | None) -> tuple[str, ...]:
        return tuple(build_metadata_command(
            ffmpeg_path, source, temporary, target_ext=target_ext, fmt=fmt,
            bitrate=edit.bitrate, cover_action=action, cover_source=cover,
            edited_values=edited_values, overwrite=overwrite or replaces_source,
        ))

    auxiliary_paths: tuple[Path, ...] = ()
    preparation_steps: tuple[ProcessStep, ...] = ()
    if cover_action is None and converting and edit.has_original_cover:
        if target_ext in COVER_UNSUPPORTED_EXTENSIONS:
            warnings.append(f"{target_ext} 容器不支持内嵌封面，转换后不保留原封面")
        else:
            original_cover = temporary_path_for(output_path.with_suffix(cover_suffix_for_codec(edit.cover_codec)))
            extraction = tuple(build_cover_export_command(ffmpeg_path, source, original_cover, edit.cover_codec))
            preparation_steps = (ProcessStep(
                extraction, timeout_seconds=60, output_path=original_cover,
                failure_warning="格式转换时提取原封面失败，输出文件可能不含封面",
                fallback_command=command_for(None, None),
            ),)
            auxiliary_paths = (original_cover,)
            cover_action, cover_source = "set", original_cover

    action_text = "转换并写入" if converting else "已修改"
    return FilePlan(
        source, output_path, temporary, command_for(cover_action, cover_source),
        overwrite=overwrite if not in_place else replaces_source, backup=edit.backup,
        replaces_source=replaces_source, converted=converting, warnings=tuple(warnings),
        preparation_steps=preparation_steps, auxiliary_paths=auxiliary_paths,
        success_message=f"{source.name} → {output_path.name} {action_text}",
    )


def build_metadata_task(
    ffmpeg_path: str, edits: list[TrackEdit], overwrite: bool, in_place: bool,
    output_root: str | Path | None,
) -> TaskPlan:
    taken = {path_key(edit.path) for edit in edits}
    files: list[FilePlan] = []
    for edit in edits:
        try:
            files.append(build_output_plan(ffmpeg_path, edit, overwrite, in_place, output_root, taken))
        except (MetadataError, OSError, ValueError) as exc:
            # 规划失败也保留在任务中，Runner 计入失败，不执行任何文件操作。
            files.append(FilePlan(edit.path, edit.path, temporary_path_for(edit.path), (),
                                  replaces_source=True, error=str(exc)))
    return TaskPlan((BatchPlan("元数据写入", tuple(files)),))
