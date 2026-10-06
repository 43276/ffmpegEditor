"""用输入快照连接应用层 Planner / Reader 与共享任务回调。"""
from __future__ import annotations

from copy import deepcopy

from app.audio.album_planner import build_album_task
from app.audio.cover_planner import build_cover_export_task
from app.audio.metadata_planner import build_metadata_task
from app.audio.reader import read_audio_edits
from app.image.planner import build_image_task
from app.video.planner import build_video_task
from app.task_runner import TaskRunner
from .worker import TaskContext, TaskOperation


def _run_plan(context: TaskContext, plan):
    return TaskRunner(
        cancel_check=context.cancel_check, finish_check=context.finish_check,
        on_log=context.on_log, on_progress=context.on_progress,
        on_statistics=context.on_statistics,
    ).run(plan)


def image_job(batches, options, capabilities) -> TaskOperation:
    batches, options = deepcopy(batches), deepcopy(options)
    return lambda context: _run_plan(context, build_image_task(batches, options, capabilities))


def video_job(batches, options, capabilities) -> TaskOperation:
    batches, options = deepcopy(batches), deepcopy(options)
    return lambda context: _run_plan(context, build_video_task(batches, options, capabilities))


def album_job(plan, overwrite: bool, ffmpeg_path: str) -> TaskOperation:
    plan = deepcopy(plan)
    return lambda context: _run_plan(context, build_album_task(plan, ffmpeg_path, overwrite))


def metadata_read_job(ffprobe_path: str, ffmpeg_path: str, files) -> TaskOperation:
    files = list(files)
    return lambda context: read_audio_edits(
        ffprobe_path, ffmpeg_path, files, cancel_check=context.cancel_check,
        on_row=context.on_row, on_log=context.on_log, on_progress=context.on_progress,
    )


def metadata_write_job(ffmpeg_path: str, edits, overwrite: bool,
                       in_place: bool, output_root: str | None) -> TaskOperation:
    edits = deepcopy(edits)
    return lambda context: _run_plan(context, build_metadata_task(
        ffmpeg_path, edits, overwrite, in_place, output_root,
    ))


def cover_export_job(ffmpeg_path: str, items, output_dir: str) -> TaskOperation:
    items = list(items)
    return lambda context: _run_plan(context, build_cover_export_task(ffmpeg_path, items, output_dir))
