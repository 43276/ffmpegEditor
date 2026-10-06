"""转换执行与尚待第二阶段迁移的图片命令入口。"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path
from contextlib import ExitStack
from typing import Callable, Sequence

from .Core import ANIMATED_EXTENSIONS
from .image.models import ConvertOptions
from .image.commands import select_image_encoder
from .errors import FfmpegError as ConverterError
from .ffmpeg_environment import FfmpegCapabilities
from .task_models import ProcessResult

# 目标 WebP 编码器单边像素上限，超出会直接报错
_WEBP_MAX_DIMENSION = 16383

# GIF 调色板颜色数范围（质量 1~100 线性映射到该区间）
_GIF_MIN_COLORS = 32
_GIF_MAX_COLORS = 256


def OutputNameFor(src: Path, target_extension: str | None) -> str:
    """输出文件名：原文件名 + 目标后缀；保持原格式时沿用源扩展名。"""
    extension = target_extension.lower() if target_extension else src.suffix
    return f"{src.stem}{extension}"


def BuildCommand(opts: ConvertOptions, capabilities: FfmpegCapabilities, src: Path) -> list[str]:
    """构建图片编码参数；实际写入路径由现有 Worker 追加。"""
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
        color_count = GifMaxColors(opts.quality)
        palette_chain = (
            "split[s0][s1];"
            f"[s0]palettegen=max_colors={color_count}[p];"
            "[s1][p]paletteuse=dither=sierra2_4a"
        )
        graph = f"{scale_filter},{palette_chain}" if scale_filter else palette_chain
        # -loop 0：GIF 无限循环
        cmd += ["-vf", graph, "-loop", "0"]
        return cmd

    if scale_filter:
        cmd += ["-vf", scale_filter]
    cmd += QualityToFfmpegArgs(extension, opts.quality)

    # -loop 0：动画 WebP 无限循环（对静态图无影响）
    if extension == ".webp":
        cmd += ["-loop", "0"]

    # libaom-av1 默认速度极慢，显式放宽编码速度（画质影响很小）
    if extension == ".avif" and encoder == "libaom-av1":
        cmd += ["-cpu-used", "6", "-row-mt", "1"]

    return cmd


def GifMaxColors(quality: int | None) -> int:
    """把统一的质量值（1~100）映射为 GIF 调色板颜色数（32~256）。

    GIF 没有 JPEG / WebP 那样的“质量”参数，画质实际由调色板大小决定，
    颜色越多画质越好、文件越大；quality 为 None 时取最大值。
    """
    if quality is None:
        return _GIF_MAX_COLORS
    quantized = max(1, min(100, quality))
    span = _GIF_MAX_COLORS - _GIF_MIN_COLORS
    return int(round(_GIF_MIN_COLORS + quantized / 100 * span))


def QualityToFfmpegArgs(extension: str, quality: int | None) -> list[str]:
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


def RunFileProcess(
    cmd: Sequence[str],
    cancel_check: Callable[[], bool] | None = None,
    timeout_seconds: float = 900.0,
    *, capture_stdout: bool = False,
) -> ProcessResult:
    """执行命令并返回结构化结果；输出用临时文件收集，避免管道阻塞。"""
    if cancel_check is not None and cancel_check():
        return ProcessResult(-1, cancelled=True)
    cancelled = False
    timed_out = False
    with ExitStack() as stack:
        error_file = stack.enter_context(tempfile.TemporaryFile())
        output_file = stack.enter_context(tempfile.TemporaryFile()) if capture_stdout else None
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=output_file if output_file is not None else subprocess.DEVNULL,
                stderr=error_file,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError as exc:
            return ProcessResult(-1, f"无法启动 ffmpeg：{exc}")
        try:
            started_at = time.monotonic()
            while proc.poll() is None:
                if cancel_check is not None and cancel_check():
                    cancelled = True
                    break
                if time.monotonic() - started_at > timeout_seconds:
                    timed_out = True
                    break
                time.sleep(0.05)
        finally:
            # 包括取消回调抛异常的情况，均不遗留正在写临时文件的子进程。
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        error_file.seek(0, os.SEEK_END)
        error_file.seek(max(0, error_file.tell() - 6000))
        stderr = error_file.read().decode("utf-8", errors="replace").strip()[-1500:]
        if timed_out:
            stderr = "处理超时，已强制终止" + (f"\n{stderr}" if stderr else "")
        stdout = b""
        if output_file is not None:
            output_file.seek(0)
            stdout = output_file.read()
        return ProcessResult(proc.returncode, stderr, cancelled, timed_out, stdout)
