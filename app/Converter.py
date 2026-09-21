"""ffmpeg 封装：探测能力、按参数构建命令行、执行单文件转换。"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .Core import ANIMATED_EXTENSIONS, ConvertOptions

# AV1 编码器优先级：libsvtavif 快，libaom-av1 是兜底
_AV1_ENCODERS: tuple[str, ...] = ("libsvtavif", "libaom-av1")

# 动画 WebP 编码器：libwebp_anim 才能保留多帧，缺失时退回单帧的 libwebp
_WEBP_ANIM_ENCODER = "libwebp_anim"

# 目标 WebP 编码器单边像素上限，超出会直接报错
_WEBP_MAX_DIMENSION = 16383

# GIF 调色板颜色数范围（质量 1~100 线性映射到该区间）
_GIF_MIN_COLORS = 32
_GIF_MAX_COLORS = 256


class ConverterError(Exception):
    """ffmpeg 相关的可预期错误，消息可直接展示给用户。"""


def LocateFfmpeg(explicit: str | None = None) -> str:
    """返回 ffmpeg 可执行文件路径；找不到时抛 ConverterError。"""
    if explicit:
        candidate = Path(explicit)
        if candidate.is_file():
            return str(candidate.resolve())
        raise ConverterError(f"ffmpeg 路径不存在：{explicit}")
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise ConverterError("未在 PATH 中找到 ffmpeg，请在界面中手动指定 ffmpeg.exe 位置")


@dataclass
class FfmpegCapabilities:
    version: str
    encoders: set[str]
    av1_encoder: str | None          # libsvtavif / libaom-av1，都不支持则为 None
    avif_ok: bool                    # 能否输出 AVIF
    gif_ok: bool                     # 能否输出 GIF

    def VideoEncoderName(
        self, target_extension: str, source_extension: str | None = None
    ) -> str:
        """根据目标扩展名返回 ffmpeg 视频编码器名，不支持时抛 ConverterError。

        source_extension 用于判断源是否可能为多帧动画（GIF / WebP）：这类源
        转 WebP 时优先使用 libwebp_anim，否则动画会被丢成单帧。
        """
        ext = target_extension.lower()
        if ext in (".jpg", ".jpeg"):
            return "mjpeg"
        if ext == ".png":
            return "png"
        if ext == ".webp":
            source_ext = (source_extension or "").lower()
            if source_ext in ANIMATED_EXTENSIONS and _WEBP_ANIM_ENCODER in self.encoders:
                return _WEBP_ANIM_ENCODER
            if "libwebp" not in self.encoders:
                raise ConverterError("当前 ffmpeg 缺少 libwebp 编码器，无法输出 WebP")
            return "libwebp"
        if ext == ".gif":
            if not self.gif_ok:
                raise ConverterError("当前 ffmpeg 缺少 gif 编码器，无法输出 GIF")
            return "gif"
        if ext == ".avif":
            if not self.avif_ok:
                raise ConverterError("当前 ffmpeg 不支持 AVIF 编码（缺少 AV1 编码器或 avif 封装器）")
            return self.av1_encoder or "libaom-av1"
        if ext == ".bmp":
            return "bmp"
        if ext == ".tiff":
            return "tiff"
        raise ConverterError(f"不支持的目标格式：{ext}")


def ProbeFfmpeg(ffmpeg_path: str) -> FfmpegCapabilities:
    """运行 ffmpeg -version / -encoders / -muxers，探测可用编码能力。"""
    try:
        version_result = subprocess.run(
            [ffmpeg_path, "-version"], capture_output=True, text=True, timeout=30
        )
        encoders_result = subprocess.run(
            [ffmpeg_path, "-encoders"], capture_output=True, text=True, timeout=30
        )
        muxers_result = subprocess.run(
            [ffmpeg_path, "-muxers"], capture_output=True, text=True, timeout=30
        )
    except OSError as exc:
        raise ConverterError(f"无法运行 ffmpeg：{exc}") from exc

    if version_result.returncode != 0:
        detail = version_result.stderr.strip() or "未知错误"
        raise ConverterError(f"ffmpeg 无法启动：{detail}")

    first_line = version_result.stdout.splitlines()[0].strip() if version_result.stdout else ffmpeg_path
    encoders = _ParseNameTokens(encoders_result.stdout)
    muxers = _ParseNameTokens(muxers_result.stdout)
    av1_encoder = next((name for name in _AV1_ENCODERS if name in encoders), None)
    return FfmpegCapabilities(
        version=first_line,
        encoders=encoders,
        av1_encoder=av1_encoder,
        avif_ok=bool(av1_encoder) and "avif" in muxers,
        gif_ok="gif" in encoders and "gif" in muxers,
    )


def _ParseNameTokens(text: str) -> set[str]:
    """解析 `ffmpeg -encoders / -muxers` 输出中的名称列。"""
    names: set[str] = set()
    for raw_line in text.splitlines():
        parts = raw_line.strip().split()
        if len(parts) < 2:
            continue
        flag, name = parts[0], parts[1]
        if ":" in flag or "=" in flag or name.startswith("="):
            continue
        if name.startswith(("-", "_")):
            continue
        names.add(name)
    return names


def OutputNameFor(src: Path, target_extension: str | None) -> str:
    """输出文件名：原文件名 + 目标后缀；保持原格式时沿用源扩展名。"""
    extension = target_extension.lower() if target_extension else src.suffix
    return f"{src.stem}{extension}"


def BuildCommand(opts: ConvertOptions, capabilities: FfmpegCapabilities, src: Path) -> list[str]:
    """为单个文件构建完整 ffmpeg 命令行。"""
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

    encoder = capabilities.VideoEncoderName(extension, src.suffix)
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
    cmd: list[str],
    cancel_check: Callable[[], bool] | None = None,
    timeout_seconds: float = 900.0,
) -> tuple[int, str, bool]:
    """执行单文件转换。

    返回 (返回码, stderr 尾部文本, 是否被取消)。ffmpeg 的 stderr 会写到
    临时文件，避免管道缓冲导致子进程阻塞，同时保证取消响应及时。
    """
    return_code = -1
    cancelled = False
    error_tail = ""

    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as error_file:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=error_file)
        except OSError as exc:
            return -1, f"无法启动 ffmpeg：{exc}", False

        started_at = time.time()
        while proc.poll() is None:
            if cancel_check is not None and cancel_check():
                cancelled = True
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                break
            if time.time() - started_at > timeout_seconds:
                proc.kill()
                proc.wait(timeout=5)
                error_tail = "处理超时，已强制终止"
                break
            time.sleep(0.05)

        return_code = proc.returncode if proc.returncode is not None else -1
        if not error_tail:
            error_file.seek(0)
            text = error_file.read().strip()
            if text:
                error_tail = text[-1500:]

    return return_code, error_tail, cancelled
