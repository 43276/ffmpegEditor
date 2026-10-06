"""完整图片转换命令与编码策略；不执行进程。"""
from __future__ import annotations

from pathlib import Path

from .formats import ANIMATED_EXTENSIONS
from .models import ConvertOptions
from app.errors import FfmpegError
from app.ffmpeg_environment import FfmpegCapabilities


AV1_ENCODERS = ("libsvtavif", "libaom-av1")


def select_image_encoder(
    capabilities: FfmpegCapabilities, target_extension: str, source_extension: str = "",
) -> str:
    extension = target_extension.lower()
    if extension in (".jpg", ".jpeg"):
        return "mjpeg"
    if extension in (".tif", ".tiff"):
        return "tiff"
    if extension in (".png", ".bmp"):
        return extension[1:]
    if extension == ".webp":
        if source_extension.lower() in {".gif", ".webp"} and "libwebp_anim" in capabilities.encoders:
            return "libwebp_anim"
        if "libwebp" not in capabilities.encoders:
            raise FfmpegError("当前 ffmpeg 缺少 libwebp 编码器，无法输出 WebP")
        return "libwebp"
    if extension == ".gif":
        if "gif" not in capabilities.encoders or "gif" not in capabilities.muxers:
            raise FfmpegError("当前 ffmpeg 缺少 gif 编码器或封装器，无法输出 GIF")
        return "gif"
    if extension == ".avif":
        encoder = next((name for name in AV1_ENCODERS if name in capabilities.encoders), None)
        if encoder is None or "avif" not in capabilities.muxers:
            raise FfmpegError("当前 ffmpeg 不支持 AVIF 编码（缺少 AV1 编码器或 avif 封装器）")
        return encoder
    raise FfmpegError(f"不支持的目标格式：{extension}")


def supports_image_format(capabilities: FfmpegCapabilities, extension: str) -> bool:
    try:
        select_image_encoder(capabilities, extension)
    except FfmpegError:
        return False
    return True


# 目标 WebP 编码器单边像素上限，超出会直接报错
_WEBP_MAX_DIMENSION = 16383

# GIF 调色板颜色数范围（质量 1~100 线性映射到该区间）
_GIF_MIN_COLORS = 32
_GIF_MAX_COLORS = 256




def build_image_command(opts: ConvertOptions, capabilities: FfmpegCapabilities, src: Path, output_path: Path) -> list[str]:
    """构建包含实际写入路径的完整命令。"""
    extension = (opts.target_extension or src.suffix).lower()
    cmd = [opts.ffmpeg_path, "-hide_banner", "-loglevel", "error"]
    cmd.append("-y" if opts.overwrite else "-n")
    cmd += ["-i", str(src)]

    # 非 GIF / WebP 的目标格式只能容纳一帧：多帧源（动画 GIF / 动画 WebP）
    # 若不限制帧数，image2 / mjpeg 会因“同名文件写入多帧”而直接失败。
    if extension not in ANIMATED_EXTENSIONS:
        cmd += ["-frames:v", "1"]

    # WebP 编码器单边上限 16383 像素，超出会直接报错；自动等比缩到上限内。
    # 注意 scale 需用 min(limit, iw/ih) 防止把小图放大。
    scale_limit = opts.max_dimension if opts.max_dimension and opts.max_dimension > 0 else None
    if extension == ".webp" and (scale_limit is None or scale_limit > _WEBP_MAX_DIMENSION):
        scale_limit = _WEBP_MAX_DIMENSION
    scale_filter = None
    if scale_limit:
        scale_filter = (
            f"scale='min({scale_limit},iw)':'min({scale_limit},ih)':"
            "force_original_aspect_ratio=decrease"
        )

    encoder = select_image_encoder(capabilities, extension, src.suffix)
    cmd += ["-c:v", encoder]

    # GIF 只有 256 色调色板：先用 palettegen 生成调色板再 paletteuse 量化，
    # 画质明显优于内建默认编码，且单帧与多帧动画都适用。
    if extension == ".gif":
        color_count = gif_max_colors(opts.quality)
        palette_chain = (
            "split[s0][s1];"
            f"[s0]palettegen=max_colors={color_count}[p];"
            "[s1][p]paletteuse=dither=sierra2_4a"
        )
        graph = f"{scale_filter},{palette_chain}" if scale_filter else palette_chain
        # -loop 0：GIF 无限循环
        cmd += ["-vf", graph, "-loop", "0"]
        return [*cmd, str(output_path)]

    if scale_filter:
        cmd += ["-vf", scale_filter]
    cmd += quality_to_ffmpeg_args(extension, opts.quality)

    # -loop 0：动画 WebP 无限循环（对静态图无影响）
    if extension == ".webp":
        cmd += ["-loop", "0"]

    # libaom-av1 默认速度极慢，显式放宽编码速度（画质影响很小）
    if extension == ".avif" and encoder == "libaom-av1":
        cmd += ["-cpu-used", "6", "-row-mt", "1"]

    return [*cmd, str(output_path)]


def gif_max_colors(quality: int | None) -> int:
    """把统一的质量值（1~100）映射为 GIF 调色板颜色数（32~256）。

    GIF 没有 JPEG / WebP 那样的“质量”参数，画质实际由调色板大小决定，
    颜色越多画质越好、文件越大；quality 为 None 时取最大值。
    """
    if quality is None:
        return _GIF_MAX_COLORS
    quantized = max(1, min(100, quality))
    span = _GIF_MAX_COLORS - _GIF_MIN_COLORS
    return int(round(_GIF_MIN_COLORS + quantized / 100 * span))


def quality_to_ffmpeg_args(extension: str, quality: int | None) -> list[str]:
    """把统一的质量值（1~100，越大画质越好文件越大）映射为各编码器参数。"""
    ext = extension.lower()
    quantized = max(1, min(100, quality)) if quality is not None else None

    if ext in (".jpg", ".jpeg"):
        if quantized is None:
            return []
        # mjpeg 的 q:v 范围 2~31，数值越小质量越高
        q_v = max(2, min(31, round(31 - quantized / 100 * 29)))
        return ["-q:v", str(q_v)]

    if ext == ".webp":
        if quantized is None:
            return ["-compression_level", "6"]
        return ["-quality", str(quantized), "-compression_level", "6"]

    if ext == ".avif":
        if quantized is None:
            return []
        # AV1 的 crf 范围 0~63，数值越小质量越高
        crf = max(0, min(63, round((100 - quantized) / 100 * 63)))
        args = ["-crf", str(crf)]
        return args

    return []
