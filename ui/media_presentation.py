"""媒体任务预览与格式列的显示文本。"""
from __future__ import annotations

from app.image.models import Batch
from app.video.models import VideoBatch
from app.audio.models import AlbumPlan, TrackEdit
from app.audio.formats import DEFAULT_BITRATE, format_by_key

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


def summarize_image_batches(batches: list[Batch]) -> str:
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


def summarize_video_batches(batches: list[VideoBatch]) -> str:
    total = sum(len(batch.files) for batch in batches)
    if len(batches) == 1:
        batch = batches[0]
        return f"将压缩 {total} 个视频，输出到：{batch.output_dir}"
    return f"发现 {len(batches)} 个待处理目录、共 {total} 个视频（首个输出目录：{batches[0].output_dir}）"


def summarize_album_plan(plan: AlbumPlan) -> str:
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


def format_display(original_ext: str, target_format_key: str | None, bitrate: str | None) -> str:
    """格式列显示文本：无转换为 .mp3；有转换为 .mp3->.m4a 192k（无损无码率）。"""
    source_ext = original_ext.lower()
    target = format_by_key(target_format_key)
    if target is None or target.key == "keep":
        return source_ext
    text = f"{source_ext}->{target.extension}"
    if target.lossy:
        text += f" {bitrate or DEFAULT_BITRATE}"
    return text


def format_audio_edit(edit: TrackEdit) -> str:
    return format_display(edit.original_ext, edit.target_format_key, edit.bitrate)
