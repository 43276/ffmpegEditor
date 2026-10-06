"""封面导出的输出命名与批次占用；不创建文件。"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .cover_commands import build_cover_export_command, cover_suffix_for_codec
from app.output_files import path_key, temporary_path_for, unique_path_for
from app.task_models import BatchPlan, FilePlan, TaskPlan


def build_cover_export_plan(
    ffmpeg_path: str, audio_path: Path, dest_dir: str | Path, cover_codec: str | None,
    taken: set[str] | None = None,
) -> FilePlan:
    destination = unique_path_for(Path(dest_dir), audio_path.stem, cover_suffix_for_codec(cover_codec), taken)
    if taken is not None:
        taken.add(path_key(destination))
    temporary = temporary_path_for(destination)
    command = build_cover_export_command(ffmpeg_path, audio_path, temporary, cover_codec)
    return FilePlan(audio_path, destination, temporary, tuple(command), overwrite=False,
                    timeout_seconds=60)


def build_cover_export_task(
    ffmpeg_path: str, items: list[tuple[Path, str | None]], output_dir: str | Path,
) -> TaskPlan:
    taken = {path_key(path) for path, _codec in items}
    files: list[FilePlan] = []
    for path, codec in items:
        plan = build_cover_export_plan(ffmpeg_path, path, output_dir, codec, taken)
        if codec is None:
            plan = replace(plan, skip_reason="无内嵌封面，跳过")
        files.append(plan)
    return TaskPlan((BatchPlan("封面导出", tuple(files)),), output_dirs=(Path(output_dir),))
