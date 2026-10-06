"""图片编码策略；其余命令构建在下一阶段迁入此模块。"""
from __future__ import annotations

from app.errors import FfmpegError
from app.ffmpeg_environment import FfmpegCapabilities


AV1_ENCODERS = ("libsvtavif", "libaom-av1")


def select_image_encoder(
    capabilities: FfmpegCapabilities, target_extension: str, source_extension: str = "",
) -> str:
    extension = target_extension.lower()
    if extension in (".jpg", ".jpeg"):
        return "mjpeg"
    if extension in (".png", ".bmp", ".tiff"):
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
