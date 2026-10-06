"""图片格式与动画、无损规则。"""
from __future__ import annotations

# 支持的图片格式（GIF / WebP 可能为多帧动画，其余均为单帧静态图）
SUPPORTED_EXTENSIONS: set[str] = {
    ".jpg", ".jpeg", ".png", ".webp", ".avif", ".bmp", ".tif", ".tiff", ".gif",
}

# 支持动画（多帧）的格式：GIF ⇄ WebP 之间可以整段互转并保留动画
ANIMATED_EXTENSIONS: set[str] = {".gif", ".webp"}


# 无损目标格式：压缩率与“质量”滑块无关
LOSSLESS_EXTENSIONS: set[str] = {".png", ".bmp", ".tif", ".tiff"}

def is_lossless_extension(extension: str | None) -> bool:
    return extension is not None and extension.lower() in LOSSLESS_EXTENSIONS
