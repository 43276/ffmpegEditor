"""FFmpeg 工具定位与能力快照，格式选择策略由业务模块负责。"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import FfmpegError


@dataclass(frozen=True)
class FfmpegCapabilities:
    version: str
    encoders: frozenset[str]
    muxers: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "encoders", frozenset(self.encoders))
        object.__setattr__(self, "muxers", frozenset(self.muxers))


def locate_ffmpeg(explicit: str | None = None) -> str:
    if explicit:
        candidate = Path(explicit)
        if candidate.is_file():
            return str(candidate.resolve())
        raise FfmpegError(f"ffmpeg 路径不存在：{explicit}")
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise FfmpegError("未在 PATH 中找到 ffmpeg，请在界面中手动指定 ffmpeg.exe 位置")


def locate_ffprobe(ffmpeg_path: str | None = None, *, explicit: str | None = None) -> str:
    if explicit:
        candidate = Path(explicit)
        if candidate.is_file():
            return str(candidate.resolve())
        raise FfmpegError(f"ffprobe 路径不存在：{explicit}")
    if ffmpeg_path:
        # 错误的显式 ffmpeg 配置不能借 PATH 中的其他工具悄悄恢复。
        base = Path(locate_ffmpeg(ffmpeg_path))
        candidate = base.with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if candidate.is_file():
            return str(candidate.resolve())
    found = shutil.which("ffprobe")
    if found:
        return found
    raise FfmpegError("未找到 ffprobe（需要与 ffmpeg 同目录或在 PATH 中）")


def parse_name_tokens(text: str) -> frozenset[str]:
    """仅解析编码器 / 封装器表的数据行，忽略说明行。"""
    names: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        flags, name = parts[:2]
        if not re.fullmatch(r"[VASD.EFSXBT]{1,6}", flags) or name == "=":
            continue
        names.update(name.split(","))
    return frozenset(names)


def probe_ffmpeg(ffmpeg_path: str) -> FfmpegCapabilities:
    options = dict(
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    outputs: dict[str, str] = {}
    for argument in ("-version", "-encoders", "-muxers"):
        try:
            result = subprocess.run([ffmpeg_path, argument], **options)
        except OSError as exc:
            raise FfmpegError(f"无法运行 ffmpeg：{exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise FfmpegError(f"ffmpeg 检测超时（{argument}）") from exc
        if result.returncode != 0:
            detail = (result.stderr or "未知错误").strip()[-1500:]
            raise FfmpegError(f"ffmpeg 检测失败（{argument}）：{detail}")
        outputs[argument] = result.stdout
    lines = outputs["-version"].splitlines()
    return FfmpegCapabilities(
        version=lines[0].strip() if lines else ffmpeg_path,
        encoders=parse_name_tokens(outputs["-encoders"]),
        muxers=parse_name_tokens(outputs["-muxers"]),
    )
