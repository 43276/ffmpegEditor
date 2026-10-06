"""用输入快照连接应用层 Planner / Reader 与共享任务回调。"""
from __future__ import annotations

from copy import deepcopy

from app.audio.album_planner import build_album_task, build_album_plan
from app.audio.cover_planner import build_cover_export_task
from app.audio.metadata_planner import build_metadata_task
from app.audio.reader import read_audio_edits, collect_audio_inputs
from app.image.planner import build_image_task, build_batches_for_inputs
from app.video.planner import build_video_task, build_video_batches_for_inputs
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
    return lambda context: _run_plan(context, build_image_task(
        batches, options, capabilities, cancel_check=context.cancel_check,
    ))


def video_job(batches, options, capabilities) -> TaskOperation:
    batches, options = deepcopy(batches), deepcopy(options)
    return lambda context: _run_plan(context, build_video_task(
        batches, options, capabilities, cancel_check=context.cancel_check,
    ))


def image_inputs_job(paths, options, capabilities, *, include_output_dirs=False,
                     output_root=None) -> TaskOperation:
    paths, options = tuple(paths), deepcopy(options)

    def operation(context):
        batches = build_batches_for_inputs(
            paths, include_output_dirs=include_output_dirs, output_root=output_root,
            cancel_check=context.cancel_check,
        )
        return _run_plan(context, build_image_task(
            batches, options, capabilities, cancel_check=context.cancel_check,
        ))
    return operation


def video_inputs_job(paths, options, capabilities, *, include_output_dirs=False) -> TaskOperation:
    paths, options = tuple(paths), deepcopy(options)

    def operation(context):
        batches = build_video_batches_for_inputs(
            paths, include_output_dirs=include_output_dirs, cancel_check=context.cancel_check,
        )
        return _run_plan(context, build_video_task(
            batches, options, capabilities, cancel_check=context.cancel_check,
        ))
    return operation


def album_inputs_job(root, overwrite: bool, ffmpeg_path: str) -> TaskOperation:
    def operation(context):
        plan = build_album_plan(root, cancel_check=context.cancel_check)
        return _run_plan(context, build_album_task(
            plan, ffmpeg_path, overwrite, cancel_check=context.cancel_check,
        ))
    return operation


def album_job(plan, overwrite: bool, ffmpeg_path: str) -> TaskOperation:
    plan = deepcopy(plan)
    return lambda context: _run_plan(context, build_album_task(
        plan, ffmpeg_path, overwrite, cancel_check=context.cancel_check,
    ))


def metadata_read_job(ffprobe_path: str, ffmpeg_path: str, files) -> TaskOperation:
    files = tuple(files)

    def operation(context):
        collected = collect_audio_inputs(files, cancel_check=context.cancel_check)
        return read_audio_edits(
            ffprobe_path, ffmpeg_path, collected, cancel_check=context.cancel_check,
            on_row=context.on_row, on_log=context.on_log, on_progress=context.on_progress,
        )
    return operation


def metadata_write_job(ffmpeg_path: str, edits, overwrite: bool,
                       in_place: bool, output_root: str | None) -> TaskOperation:
    edits = deepcopy(edits)
    return lambda context: _run_plan(context, build_metadata_task(
        ffmpeg_path, edits, overwrite, in_place, output_root, cancel_check=context.cancel_check,
    ))


def cover_export_job(ffmpeg_path: str, items, output_dir: str) -> TaskOperation:
    items = list(items)
    return lambda context: _run_plan(context, build_cover_export_task(
        ffmpeg_path, items, output_dir, cancel_check=context.cancel_check,
    ))
